"""Related-links enrichment — lightweight cross-file association.

Adds a `related` frontmatter field to each sources/*.md: up to N links to
other files that share the most discriminative keywords, measured by Jaccard
similarity over each file's keyword set. This is the **zero-cost** alternative
to the optional GraphRAG layer (which uses an LLM to extract real entity
relations, slow and expensive): related-links needs no LLM, no embeddings, no
extra parsing — it only does set arithmetic over the keywords the
knowledge_map already extracted and TF-IDF-filtered.

Runs AFTER the main pipeline loop and after knowledge_map is built (same place
and same `knowledge_map` data source as tags_enrichment), so the corpus-wide
keyword signal exists. Reuses tags_enrichment's frontmatter parse/serialize so
the body stays byte-for-byte unchanged and re-runs are idempotent.

Algorithm — Jaccard, not raw shared-count:
  shared-count alone favours keyword-rich files (they share something with
  everyone → noisy "related to all"). Jaccard = |A∩B| / |A∪B| normalizes by
  each file's own keyword count, so a file is "related" only when its keyword
  set genuinely resembles the other's — removing the big-file bias. The
  keywords are already TF-IDF-filtered upstream, so Jaccard over them captures
  the bulk of what a heavier cosine/BM25 vector model would, at a fraction of
  the cost. The metric is pluggable via config (`algorithm`) but only
  `jaccard` is implemented today — the seam exists, we don't over-build it.

Honest limits (stated, not hidden):
  - Lexical, not semantic — and the gap is wider than synonyms. Not only does
    "user" vs "利用者" not match; two files on the SAME topic that use
    DIFFERENT wording won't link at all. (Verified empirically: two government
    specs — one describing a learning system's features, one about Excel-ifying
    spec layouts — share almost no keywords and score ~0, even after adding
    body text to the keyword pool. Their shared identity as "government specs"
    lives only at the semantic layer, which no lexical metric can reach.)
    Lexical Jaccard catches files with overlapping wording (e.g. the same
    document set); true topic/semantic association is GraphRAG's job.
  - For a *suggestion* field this is acceptable — a missed or extra related
    link costs the agent one glance, not a wrong answer. related is a lead,
    not an answer; don't read "no related" as "nothing is related".
  - File-level, not concept-level associations (DocIngest is one-md-per-input).

Links use OKF-compatible bundle-relative form (`/sources/<file>.md`) so the
field is consumable by any OKF-aware reader, though the *source* of the link
is our cheap Jaccard, not OKF's LLM-derived semantic relations.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import get_nested

# Reuse the proven, byte-stable frontmatter helpers — do NOT reimplement.
from .tags_enrichment import _parse_frontmatter, _serialize_frontmatter

logger = logging.getLogger(__name__)


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity |A∩B| / |A∪B|. 0.0 when either set is empty or
    there is no union (avoids division by zero)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    union = len(a | b)
    return inter / union if union else 0.0


def _link_for(file_entry: dict[str, Any]) -> str | None:
    """Build one markdown link to a related file, OKF bundle-relative form
    (`[Title](/sources/<file>.md)`). Returns None if the entry lacks a path."""
    rel_path = file_entry.get("path", "")
    if not rel_path:
        return None
    # Display text: prefer title, fall back to the filename stem.
    title = file_entry.get("title") or Path(rel_path).stem
    # Bundle-relative absolute path (leading "/") — OKF §5.1 recommended form,
    # stable regardless of where the consumer mounts the bundle.
    href = "/" + str(rel_path).replace("\\", "/").lstrip("/")
    return f"[{title}]({href})"


def enrich_sources_with_related(
    knowledge_map: dict[str, Any],
    output_dir: Path,
    config: dict[str, Any],
) -> int:
    """Add a `related` frontmatter list (top-N Jaccard-similar files) to each
    sources/*.md. Returns the number of files modified.

    Best-effort: any single-file failure is logged at debug and skipped; the
    pipeline is never broken by this pass.
    """
    if not get_nested(config, "output.derived_metadata.related.enabled", False):
        return 0

    max_links = int(get_nested(config, "output.derived_metadata.related.max_links", 3))
    if max_links <= 0:
        return 0
    min_sim = float(
        get_nested(config, "output.derived_metadata.related.min_similarity", 0.15)
    )
    algorithm = str(
        get_nested(config, "output.derived_metadata.related.algorithm", "jaccard")
    ).strip().lower()
    if algorithm != "jaccard":
        # Only jaccard is implemented; unknown value → fail loud in the log and
        # skip (don't silently behave like a different metric).
        logger.warning(
            f"related_enrichment: unknown algorithm {algorithm!r}; "
            f"only 'jaccard' is implemented — skipping related links."
        )
        return 0

    files = knowledge_map.get("files", [])
    # Build (entry, keyword-set) once. Files without keywords can't be scored
    # and are simply never proposed as related (and get no related of their own).
    scored: list[tuple[dict[str, Any], set[str]]] = []
    for f in files:
        kws = f.get("keywords")
        kw_set = {str(k).strip().lower() for k in kws if str(k).strip()} if isinstance(kws, list) else set()
        scored.append((f, kw_set))

    modified = 0
    for i, (entry, kw_set) in enumerate(scored):
        rel_path = entry.get("path", "")
        if not rel_path or not kw_set:
            continue
        md_path = output_dir / rel_path
        if not md_path.exists():
            continue

        # Score against every OTHER file; keep those above the threshold.
        candidates: list[tuple[float, dict[str, Any]]] = []
        for j, (other, other_kw) in enumerate(scored):
            if j == i or not other_kw:
                continue
            sim = _jaccard(kw_set, other_kw)
            if sim >= min_sim:
                candidates.append((sim, other))

        if not candidates:
            continue
        # Highest similarity first; tie-break by path for deterministic output.
        candidates.sort(key=lambda c: (-c[0], c[1].get("path", "")))
        links = []
        for _sim, other in candidates[:max_links]:
            link = _link_for(other)
            if link:
                links.append(link)
        if not links:
            continue

        if _write_related(md_path, links):
            modified += 1

    return modified


def _write_related(md_path: Path, links: list[str]) -> bool:
    """Set frontmatter `related` to `links` on one file. Idempotent: if the
    existing `related` already equals `links`, no write. Body byte-preserved.
    Returns True iff the file was modified."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.debug(f"related_enrichment: cannot read {md_path}: {e}")
        return False

    parsed = _parse_frontmatter(text)
    if parsed is None:
        return False
    data, body_offset = parsed

    if data.get("related") == links:
        return False  # already up to date — idempotent no-op

    data["related"] = links
    new_text = _serialize_frontmatter(data) + text[body_offset:]
    try:
        md_path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        logger.debug(f"related_enrichment: cannot write {md_path}: {e}")
        return False
    return True
