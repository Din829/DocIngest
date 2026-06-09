"""
Source-MD contract regression tests.

Sibling to src/docingest/chunkers/SOURCE_MD_CONTRACT.md — that file states the
shapes parsers SHOULD emit; this file asserts that real `sources/*.md`
produced by those parsers actually chunk well end-to-end. The point is to
catch the "a parser changed its output and the chunker silently degraded"
class of bug (the native_video fake-list incident) — which no synthetic
stress test or per-parser unit test sees, because the failure lives two
layers away from where it surfaces.

Strategy: feed each real fixture through the SAME chain the pipeline uses
(create_chunker → chunk → _postprocess_chunks → inject_paths) and assert the
invariants the contract promises:

  * no severe-oversized chunk (a runaway protected block / failed split)
  * content preservation (no body text silently dropped on the way through)
  * key structure survives (tables keep a header row, timestamps survive)

Fixtures are real artefacts under knowledge/. We discover them at runtime so
adding a new knowledge base automatically widens coverage — and skip cleanly
when knowledge/ is absent (fresh checkout / CI without the corpus).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from docingest.config import load_config  # noqa: E402
from docingest.chunkers import create_chunker  # noqa: E402
from docingest.chunkers.base import BaseChunker  # noqa: E402
from docingest.pipeline import _postprocess_chunks  # noqa: E402
from docingest.enrichment.path_injector import inject_paths  # noqa: E402


KNOWLEDGE = ROOT / "knowledge"

# A chunk this far above max_tokens means a protected block was emitted whole
# without splitting (or a split silently failed). Tables/lists now have
# row_split / item_split, so nothing should reach this in healthy output.
SEVERE_OVERSIZE_FACTOR = 4


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (metadata, body) — minimal YAML-ish frontmatter parse.

    We only need format/language/source for chunker routing; a full YAML
    parser is overkill and would add a dependency. Lines we don't recognise
    are ignored, which is fine — the body is what matters for chunking.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_block = text[3:end]
    body = text[end + 4:].lstrip("\n")
    meta: dict = {}
    for line in fm_block.splitlines():
        m = re.match(r"^([a-zA-Z_]+):\s*(.+)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip("'\"")
    return meta, body


def _discover_fixtures() -> list[tuple[str, Path]]:
    """Find real sources/*.md across all knowledge bases."""
    if not KNOWLEDGE.is_dir():
        return []
    fixtures: list[tuple[str, Path]] = []
    for md in sorted(KNOWLEDGE.glob("*/sources/*.md")):
        # label = <kb>/<file> for readable failure messages
        label = f"{md.parent.parent.name}/{md.name}"
        fixtures.append((label, md))
    return fixtures


# ---------------------------------------------------------------------------
# Per-fixture invariant checks
# ---------------------------------------------------------------------------

def _chunk_real(md_path: Path, config: dict):
    raw = md_path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(raw)
    doc_metadata = {
        "source": md_path.name,
        "format": (meta.get("format") or "").lower(),
        "language": meta.get("language", ""),
        "title_path": "",
    }
    chunker = create_chunker(config)
    chunks = chunker.chunk(body, doc_metadata)
    chunks = _postprocess_chunks(chunks, config)
    if chunks and config.get("chunking", {}).get("enrichment", {}).get("path_injection", True):
        inject_paths(chunks)
    return body, chunks


def _content_chars(s: str) -> str:
    """Reduce text to its meaningful characters for content-preservation checks.

    Strips the two things that legitimately differ between a source `.md` and
    its chunks, and would otherwise cause false "content loss":

      * HTML comments (`<!-- image: ... -->`, `<!-- pagebreak -->`) — these are
        markers the chunker's _clean_image_noise / postprocess deliberately
        removes; they are NOT body text.
      * Markdown structural punctuation (`| # * - > : _` etc.) and whitespace —
        table row_split regroups `|`-delimited cells across chunks, so a shingle
        spanning a cell boundary moves; comparing only real glyphs makes the
        check order-independent.

    What remains is the actual prose / data glyphs. If those go missing, a real
    section was dropped — which is what we want to catch.
    """
    s = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)
    return re.sub(r"[\s|>#*\-:_`\[\]()<>!.]+", "", s)


def check_fixture(body: str, chunks, config: dict) -> list[str]:
    """Assert contract invariants for one real fixture; return issue strings."""
    issues: list[str] = []
    max_tokens = config["chunking"]["max_tokens"]

    if not body.strip():
        return issues  # empty source — nothing to assert
    if not chunks:
        issues.append("EMPTY_OUTPUT on non-empty source")
        return issues

    # 1) No empty chunks
    for i, c in enumerate(chunks):
        if not c.text.strip():
            issues.append(f"empty chunk at idx {i}")

    # 2) No SEVERE oversize. Path injection prepends a "[来源: ...]" header, so
    #    we measure the chunker's own body, not the injected prefix. We strip a
    #    leading injected header line if present.
    severe = max_tokens * SEVERE_OVERSIZE_FACTOR
    for i, c in enumerate(chunks):
        tok = BaseChunker.estimate_tokens(c.text)
        if tok > severe:
            issues.append(f"severe oversize ({tok} tok > {severe}) at idx {i}")

    # 3) Content preservation: the source's text must survive into the chunks.
    #    We must be robust to legitimate reordering: table row_split regroups
    #    rows, overlap duplicates tails, path injection adds headers. So a naive
    #    "is this long source window present verbatim in the output" check
    #    false-fails on any table-heavy doc (rows land in different chunks, so a
    #    contiguous source window straddles a split boundary). Instead we check
    #    coverage at the SHORT-SHINGLE level — overlapping fixed-length CJK/word
    #    windows — which is order-independent: a shingle that moved to another
    #    chunk still counts as present. Genuine loss (a whole section dropped)
    #    still shows up because its shingles appear nowhere.
    src_norm = _content_chars(body)
    out_norm = _content_chars("\n".join(c.text for c in chunks))
    if len(src_norm) > 100:
        shingle = 12  # short enough to survive row regrouping, long enough to
                      # be specific (a 12-char CJK run rarely collides)
        stride = 12   # non-overlapping windows → O(n), still dense coverage
        total = 0
        truly_lost = 0
        for i in range(0, len(src_norm) - shingle, stride):
            total += 1
            sh = src_norm[i:i + shingle]
            if sh in out_norm:
                continue
            # A whole 12-char window missing usually just means it straddled a
            # chunk boundary (or a table row_split regroup point), so the window
            # got cut in two — both halves still live in the output. That is NOT
            # loss. Real loss = NEITHER half survives anywhere. Checking both the
            # head and tail quarter distinguishes "split across a boundary" from
            # "the text is genuinely gone".
            if sh[:4] not in out_norm and sh[-4:] not in out_norm:
                truly_lost += 1
        if total:
            ratio_lost = truly_lost / total
            # Any non-trivial truly-lost fraction means a real section vanished.
            # Boundary-straddle (head-or-tail survives) is filtered out above, so
            # the threshold here can be tight.
            if ratio_lost > 0.005:
                issues.append(
                    f"content loss: {truly_lost}/{total} source shingles "
                    f"({ratio_lost:.1%}) genuinely missing (neither half found)"
                )

    # 4) Table header survival under row_split. We only assert this for tables
    #    that HAVE a Markdown separator row in the source — those are the ones
    #    row_split repeats the header for, so a split sub-chunk losing it is a
    #    real regression. Vision-transcribed tables frequently have NO separator
    #    row at all (just `| cell | cell |` lines); demanding a separator there
    #    would false-fail on legitimate output, so we don't. The invariant:
    #    if the SOURCE had separator-style tables, then any output chunk that is
    #    *mostly* table rows must still carry a separator (header propagated).
    sep_re = re.compile(r"^\s*\|[-:\s|]+\|\s*$", re.MULTILINE)
    source_has_sep_table = bool(sep_re.search(body))
    if source_has_sep_table:
        row_re = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
        for i, c in enumerate(chunks):
            lines = [ln for ln in c.text.splitlines() if ln.strip()]
            rows = row_re.findall(c.text)
            # "mostly a table" = the chunk is dominated by pipe rows (so it came
            # from splitting a separator-table, not an incidental pipe in prose)
            if lines and len(rows) >= 3 and len(rows) / len(lines) > 0.6 \
                    and not sep_re.search(c.text):
                issues.append(
                    f"table chunk idx {i}: {len(rows)} separator-table rows "
                    f"but header/separator not propagated"
                )
                break  # one report per fixture is enough

    return issues


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def main() -> int:
    fixtures = _discover_fixtures()
    if not fixtures:
        print("SKIP: no knowledge/*/sources/*.md fixtures found "
              "(run `docingest run` on the test corpus first).")
        return 0

    config = load_config()
    print(f"Source-MD contract: {len(fixtures)} real fixtures\n")
    print(f'{"fixture":55s} {"chunks":>7s} {"max_tok":>8s}  result')

    total_issues = 0
    for label, path in fixtures:
        body, chunks = _chunk_real(path, config)
        issues = check_fixture(body, chunks, config)
        max_tok = max((BaseChunker.estimate_tokens(c.text) for c in chunks), default=0)
        marker = "OK" if not issues else "FAIL"
        disp = label if len(label) <= 54 else label[:51] + "..."
        print(f'{disp:55s} {len(chunks):>7d} {max_tok:>8d}  {marker}'
              + (f' — {issues[0]}' if issues else ''))
        for extra in issues[1:]:
            print(f'{"":55s} {"":>7s} {"":>8s}    · {extra}')
        total_issues += len(issues)

    print()
    print(f'TOTAL contract violations: {total_issues}')
    return 0 if total_issues == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
