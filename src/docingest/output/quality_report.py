"""
Quality report — scan sources/*.md for Vision uncertainty markers and
table-cell number damage.

Two independent dimensions, deliberately NOT merged into one score:

1. Vision uncertainty markers — the AI honestly flagging what it could not
   read:
     - `[?]`            — partial read (e.g. "¥1,234,5[?]")
     - `[unreadable]`   — truly illegible content
   These say "the SOURCE was unreadable", not "we parsed it wrong".

2. Number fragments in table cells — a parser splitting a number across
   cell boundaries (`571` landing as `|71 5|`). These say "we DID parse it
   wrong", and unlike case 1 they are SILENT: the output looks like valid
   data, so nothing downstream can tell `71` used to be `571`. Detection is
   the only defence.

Only dimension 1 feeds `quality_score` — see its docstring for why.

Stateless, pure post-processing — reads from disk, writes a JSON report,
returns a summary dict. No dependency on pipeline internals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# Regex patterns for the uncertainty markers used by the Vision prompt.
# [?]         — partial read marker (e.g. "¥1,234,5[?]")
# [unreadable] — fully illegible marker. The Vision prompt allows these forms:
#   [unreadable]
#   [unreadable: top-left node]     (colon + descriptive suffix)
#   [unreadable node]               (space + role hint, from flowchart rules)
# Regex accepts anything inside the brackets that starts with "unreadable"
# (case-insensitive) so all current and reasonable future variants match.
_QUESTION_RE = re.compile(r"\[\?\]")
_UNREADABLE_RE = re.compile(r"\[unreadable\b[^\]]*\]", re.IGNORECASE)
# Pages whose Vision call failed outright (timeout / API error after retries).
# The pipeline writes `<!-- vision-failed page=N -->` into the page's section
# so the artefact records its own gaps — the page keeps its Docling text, only
# the Vision enrichment is missing.
_VISION_FAILED_RE = re.compile(r"<!-- vision-failed page=(\d+)[^>]*-->")

# --- Number-fragment detection (table cells) -------------------------------
# A parser that mis-places a table column boundary cuts numbers in half and
# scatters the pieces into neighbouring cells. Measured signature (from a
# comparison run against an alternative PDF engine on IEA WEO tables):
#     source  | 0.9 | 0.5 | 0.4 | 513 | 543 | 571 | 83 | 86 | 89 |
#     damaged | 0.9 | .4 0.5 | 0 | 513 | 543 | 71 5 | 83 | 9 86 | 8 |
# i.e. a cell holding two space-separated numerics where at least one piece
# is a 1-2 digit stub or starts with a decimal point.
#
# The one legitimate pattern that looks identical is the SPACE THOUSANDS
# SEPARATOR (`1 494`, `8 091`) used by IEA/EU-style documents — every
# trailing group is exactly 3 digits. It must be excluded first or reports
# on such documents drown in false positives. (Verified: excluding it takes
# false positives on our own four sample artefacts from 3 to 0 across 1011
# table rows, while still catching 88 real fragments in the damaged file.)
_SPACE_THOUSANDS_RE = re.compile(r"^\d{1,3}(?: \d{3})+$")
_NUMERIC_PIECE_RE = re.compile(r"[\d.]+")
_LEADING_DOT_RE = re.compile(r"\.\d+")
# Table-body row: starts with a pipe and is not the |---|---| separator.
_SEPARATOR_CHARS = set("|-: ")


def _is_number_fragment(cell: str) -> bool:
    """
    True when a table cell looks like a number that got cut in half.

    Rules (deliberately narrow — a false positive here sends someone
    hunting a bug that does not exist):
      - space-thousands groups (`1 494`) are legitimate → never a fragment
      - two or more space-separated numeric pieces where any piece is a
        1-2 digit stub or starts with `.` → fragment (`71 5`, `.4 0.5`)
      - a lone `.4`-style leading-dot remnant → fragment
    """
    cell = cell.strip()
    if not cell or _SPACE_THOUSANDS_RE.match(cell):
        return False

    pieces = cell.split()
    if len(pieces) >= 2 and all(_NUMERIC_PIECE_RE.fullmatch(p) for p in pieces):
        return any(
            len(p.replace(".", "")) <= 2 or p.startswith(".") for p in pieces
        )
    return bool(_LEADING_DOT_RE.fullmatch(cell))


def scan_number_fragments(text: str, max_samples: int = 20) -> dict[str, Any]:
    """
    Find table cells whose numbers appear to have been split by a bad
    column boundary.

    Returns {"count": int, "samples": [{"line": int, "cell": str, "row": str}]}.
    A non-zero count means the extracted table DATA is wrong, not merely
    hard to read — worth surfacing separately from Vision markers.
    """
    count = 0
    samples: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= _SEPARATOR_CHARS:
            continue
        for cell in stripped.strip("|").split("|"):
            if _is_number_fragment(cell):
                count += 1
                if max_samples <= 0 or len(samples) < max_samples:
                    samples.append({
                        "line": line_no,
                        "cell": cell.strip(),
                        "row": stripped[:200],
                    })
    return {"count": count, "samples": samples}


def scan_file(md_path: Path, max_samples: int = 50) -> dict[str, Any]:
    """
    Count uncertainty markers in a single Markdown file.

    Args:
        md_path: Path to a sources/*.md file.
        max_samples: Cap on how many marker lines to include in `samples`
            (display only — the full counts in question_count / unreadable_count
            are always exact). <= 0 means no cap (every marker line is sampled).

    Returns:
        Dict with counts and context lines. Empty counts if file is clean.
        `number_fragment_count` / `number_fragments` carry the table-cell
        damage findings (see scan_number_fragments) — a separate dimension
        from the Vision markers, never folded into them.
    """
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception as e:
        return {
            "file": str(md_path),
            "error": f"read failed: {e}",
            "question_count": 0,
            "unreadable_count": 0,
            "vision_failed_pages": [],
            "number_fragment_count": 0,
            "number_fragments": [],
            "samples": [],
        }

    q_matches = _QUESTION_RE.findall(text)
    u_matches = _UNREADABLE_RE.findall(text)
    vision_failed_pages = sorted({int(p) for p in _VISION_FAILED_RE.findall(text)})

    # Collect sample lines (context) for the markers, up to max_samples.
    # These are display aids only — the exact totals live in question_count /
    # unreadable_count above (full findall). max_samples <= 0 disables the cap.
    samples: list[dict[str, Any]] = []
    if q_matches or u_matches:
        lines = text.split("\n")
        for line_no, line in enumerate(lines, 1):
            if _QUESTION_RE.search(line) or _UNREADABLE_RE.search(line):
                samples.append({
                    "line": line_no,
                    "text": line.strip()[:200],
                })
                if max_samples > 0 and len(samples) >= max_samples:
                    break

    fragments = scan_number_fragments(text)

    return {
        "file": str(md_path),
        "question_count": len(q_matches),
        "unreadable_count": len(u_matches),
        "vision_failed_pages": vision_failed_pages,
        "number_fragment_count": fragments["count"],
        "number_fragments": fragments["samples"],
        "samples": samples,
    }


def generate_report(
    sources_dir: Path,
    output_path: Path | None = None,
    max_samples: int = 50,
) -> dict[str, Any]:
    """
    Scan all Markdown files under sources_dir and build an aggregate report.

    Args:
        sources_dir: Directory containing sources/*.md files (the knowledge/sources/).
        output_path: If provided, write the full report as JSON here.
        max_samples: Per-file cap on `samples` entries (display only; the
            total_questions / total_unreadable counts stay exact regardless).
            <= 0 means list every marker line. Default 50.

    Returns:
        Aggregate summary dict with keys:
          - total_files: total md files scanned
          - files_with_issues: count of files containing any marker
          - total_questions: sum of [?] markers across all files
          - total_unreadable: sum of [unreadable] markers
          - files: per-file details (only files with issues)
          - total_number_fragments: table cells whose numbers look split by a
            bad column boundary. A PARSE-CORRECTNESS signal — deliberately
            NOT folded into quality_score, which measures the opposite thing
            (Vision honestly flagging an unreadable source). Any non-zero
            value here means extracted data is wrong and silently plausible.
          - quality_score: 0.0-1.0, 1.0 = zero uncertainty. INFORMATIONAL
            ONLY — a heuristic marker count, NOT a parse-failure signal: a
            low score often just means the source itself is illegible (blur,
            stamps/watermarks, shrunk-down screenshots, handwriting) that
            even a human couldn't recover, and Vision honestly flagged it
            rather than guessing.
          - score_note: a string copy of the disclaimer above, written into
            the JSON so downstream readers see it without reading this source.
    """
    if not sources_dir.exists():
        return {
            "total_files": 0,
            "files_with_issues": 0,
            "total_questions": 0,
            "total_unreadable": 0,
            "total_number_fragments": 0,
            "files": [],
            "quality_score": 1.0,
        }

    md_files = sorted(sources_dir.rglob("*.md"))
    all_files: list[dict[str, Any]] = []
    files_with_issues: list[dict[str, Any]] = []
    total_questions = 0
    total_unreadable = 0
    total_vision_failed = 0
    total_fragments = 0

    for md in md_files:
        result = scan_file(md, max_samples=max_samples)
        all_files.append(result)
        total_questions += result["question_count"]
        total_unreadable += result["unreadable_count"]
        total_vision_failed += len(result["vision_failed_pages"])
        total_fragments += result["number_fragment_count"]
        if (
            result["question_count"] > 0
            or result["unreadable_count"] > 0
            or result["vision_failed_pages"]
            or result["number_fragment_count"] > 0
        ):
            files_with_issues.append(result)

    # Quality score: simple heuristic based on markers per file.
    # 1.0 = no markers. Each [unreadable] is weighted 2x a [?].
    total_weighted = total_questions + (total_unreadable * 2)
    if len(md_files) == 0:
        score = 1.0
    elif total_weighted == 0:
        score = 1.0
    else:
        # Normalize: ~10 markers per file average → 0.5 score
        avg_weighted = total_weighted / max(len(md_files), 1)
        score = max(0.0, 1.0 - (avg_weighted / 20.0))

    report = {
        "version": 1,
        "total_files": len(md_files),
        "files_with_issues": len(files_with_issues),
        "total_questions": total_questions,
        "total_unreadable": total_unreadable,
        # Pages whose Vision call failed outright (kept Docling text, no
        # enrichment) — distinct from the marker counts above: those are
        # honest "source is illegible" flags, this is "the call didn't run".
        # Not folded into quality_score (a different failure dimension).
        "total_vision_failed_pages": total_vision_failed,
        # Table cells whose numbers look cut in half by a mis-placed column
        # boundary. Also NOT in quality_score, for the opposite reason to the
        # Vision markers: those flag an honestly-unreadable source, this flags
        # data we extracted WRONG while looking right. Mixing the two would let
        # a clean-Vision run hide broken table data behind a 1.00 score.
        "total_number_fragments": total_fragments,
        "quality_score": round(score, 3),
        # Inline disclaimer so anyone reading this JSON later (a human or an
        # agent) gets the same context the CLI prints at run time, without
        # having to read the source. The score is a heuristic marker count,
        # NOT a parse-failure signal — and it deflates on a single large
        # document (all markers divided by one file), so a low number here
        # routinely means "the source had illegible bits Vision honestly
        # flagged", not "the parse went wrong".
        "score_note": (
            "Informational only — a heuristic count of Vision uncertainty "
            "markers ([?] partial, [unreadable] gaps), NOT a parse-failure "
            "signal. A low score is often the source itself being unreadable "
            "(watermarks, low-res scans, vertical margin text) that Vision "
            "honestly flagged rather than guessing; it can also deflate on a "
            "single large document where every marker is averaged over one "
            "file. Judge extraction quality by inspecting sources/*.md and "
            "the 'files'/'samples' below, not by this number alone."
        ),
        "files": files_with_issues,  # only files with issues; clean files omitted
    }

    if output_path is not None:
        try:
            output_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    return report


def format_summary(report: dict[str, Any]) -> str:
    """
    Format the report as a short human-readable summary string.

    Used by the CLI to print a one-section quality overview after a run.
    """
    total = report.get("total_files", 0)
    issues = report.get("files_with_issues", 0)
    q = report.get("total_questions", 0)
    u = report.get("total_unreadable", 0)
    vf = report.get("total_vision_failed_pages", 0)
    frag = report.get("total_number_fragments", 0)
    score = report.get("quality_score", 1.0)

    if total == 0:
        return "No files scanned"

    # Failed Vision pages are a call-level outage, not an honesty marker —
    # always worth a line of their own when present.
    vf_note = (
        f"; {vf} page(s) failed Vision outright (kept text, no enrichment "
        f"— see vision_failed_pages in quality_report.json)"
        if vf else ""
    )

    # Number fragments are the one finding here that means "the DATA is
    # wrong". Unlike the markers, nothing downstream can notice it on its
    # own — a split `571` reads as a perfectly valid `71`. So it gets said
    # even on an otherwise-clean run, where the old wording was "clean".
    frag_note = (
        f"; WARNING: {frag} table cell(s) contain split numbers "
        f"(a column boundary cut values in half — extracted data is wrong, "
        f"see number_fragments in quality_report.json)"
        if frag else ""
    )

    if q == 0 and u == 0:
        return (
            f"Quality: clean ({total} files, zero uncertainty markers)"
            f"{vf_note}{frag_note}"
        )

    pct = issues * 100 // max(total, 1)
    return (
        f"Quality: {issues}/{total} files ({pct}%) have uncertainty markers "
        f"— {q} [?] partial reads, {u} [unreadable] gaps "
        f"(score: {score:.2f}; informational only — a marker count, not a "
        f"parse-failure signal: a low score is often the source itself being "
        f"unreadable, which Vision honestly flagged rather than guessing)"
        f"{vf_note}{frag_note}"
    )
