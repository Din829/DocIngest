"""Description enrichment — one retrieval-optimized sentence per file.

Adds a `description` frontmatter field (OKF-recommended: "a one to two
sentence summary optimized for search & retrieval") to each sources/*.md.
Agents grep frontmatter to pre-filter files without opening bodies; a good
description is the highest-leverage line for that pass.

Unlike tags/related (zero-cost set arithmetic), a useful description needs
an LLM — so this pass is default-OFF (config:
`output.derived_metadata.description.enabled`), mirroring the related-links
precedent: derived fields that cost nothing default on, fields that cost an
API call default off.

Cost shape: ONE text_completion call per batch of files (default 20), fed
only compact signals (title, sections, keywords, a short body excerpt) —
not full documents. Runs after knowledge_map is built, same mount point and
same data source as tags/related enrichment. Reuses tags_enrichment's
byte-stable frontmatter helpers; re-runs are idempotent, and files that
already carry a description are skipped (AI output is not re-rolled on
every incremental run).

Failure posture: best-effort. A failed batch logs a warning and skips —
the pipeline never breaks over a missing description.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from ..config import get_nested

# Reuse the proven, byte-stable frontmatter helpers — do NOT reimplement.
from .tags_enrichment import _parse_frontmatter, _serialize_frontmatter

logger = logging.getLogger(__name__)

# Body excerpt length fed to the LLM per file. Enough to see what the file
# actually is; small enough that a 20-file batch stays a few K tokens.
_EXCERPT_CHARS = 400


def _body_excerpt(md_path: Path) -> str:
    """Representative body excerpt (frontmatter stripped, whitespace
    collapsed): the opening _EXCERPT_CHARS plus _EXCERPT_CHARS from the
    middle of the document. Long files often open with title/intro/promo
    boilerplate (video descriptions, cover pages); the mid-document slice
    shows what the content actually is. Empty string on any read problem —
    the file is then described from its structural signals alone."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    parsed = _parse_frontmatter(text)
    body = text[parsed[1]:] if parsed is not None else text
    flat = " ".join(body.split())
    if len(flat) <= _EXCERPT_CHARS * 2:
        return flat
    mid = len(flat) // 2
    return flat[:_EXCERPT_CHARS] + " […] " + flat[mid:mid + _EXCERPT_CHARS]


def _build_prompt(entries: list[dict[str, Any]]) -> str:
    """Numbered-list prompt for one batch. Numeric keys make the reply
    robust to filename quirks (emoji titles, CJK, spaces)."""
    lines = []
    for i, e in enumerate(entries, 1):
        line = f"{i}. {e['original']} ({e['format']}, {e.get('language', '?')})"
        if e.get("sections"):
            line += " sections=[" + ", ".join(str(s) for s in e["sections"][:8]) + "]"
        if e.get("keywords"):
            line += " keywords=[" + ", ".join(str(k) for k in e["keywords"][:10]) + "]"
        if e.get("excerpt"):
            line += f'\n   excerpt: "{e["excerpt"]}"'
        lines.append(line)

    return f"""For each numbered file below, write ONE search-retrieval description:
1-2 plain sentences stating what the file contains and what questions it can
answer. Write in the SAME language as that file's content.

Rules:
- Summarize in YOUR OWN words. Do NOT copy the file's own title, intro,
  or promotional self-description verbatim — that text may appear at the
  start of the excerpt and is usually marketing, not substance.
- No hashtags, no emoji, no "this file/document contains" boilerplate.
- State the actual topics covered and the questions the content can answer.

Files:
{chr(10).join(lines)}

Output as valid YAML mapping each NUMBER to its description (no markdown
fences, no explanation):
1: ...
2: ...
"""


def _write_description(md_path: Path, description: str) -> bool:
    """Set frontmatter `description` on one file. Returns True iff modified."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.debug(f"description_enrichment: cannot read {md_path}: {e}")
        return False

    parsed = _parse_frontmatter(text)
    if parsed is None:
        return False
    data, body_offset = parsed

    if data.get("description") == description:
        return False  # idempotent no-op

    data["description"] = description
    new_text = _serialize_frontmatter(data) + text[body_offset:]
    try:
        md_path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        logger.debug(f"description_enrichment: cannot write {md_path}: {e}")
        return False
    return True


def enrich_sources_with_descriptions(
    knowledge_map: dict[str, Any],
    output_dir: Path,
    config: dict[str, Any],
) -> int:
    """Add a `description` frontmatter field to each sources/*.md that lacks
    one. Returns the number of files modified."""
    if not get_nested(config, "output.derived_metadata.description.enabled", False):
        return 0

    batch_size = int(
        get_nested(config, "output.derived_metadata.description.batch_size", 20)
    )
    if batch_size <= 0:
        return 0

    # Collect files that need a description. Existing descriptions are kept —
    # regenerating them on every run would churn AI output for no gain.
    todo: list[dict[str, Any]] = []
    for f in knowledge_map.get("files", []):
        rel_path = f.get("path", "")
        if not rel_path:
            continue
        md_path = output_dir / rel_path
        if not md_path.exists():
            continue
        try:
            parsed = _parse_frontmatter(md_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if parsed is None or parsed[0].get("description"):
            continue
        todo.append({
            "md_path": md_path,
            "original": f.get("original", Path(rel_path).stem),
            "format": f.get("format", "?"),
            "language": f.get("language"),
            "sections": f.get("sections"),
            "keywords": f.get("keywords"),
            "excerpt": _body_excerpt(md_path),
        })

    if not todo:
        return 0

    from ..models.provider import text_completion

    model_config = get_nested(config, "models.chunking_assist", {})
    modified = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        try:
            response, finish_reason = text_completion(
                prompt=_build_prompt(batch),
                model_config=model_config,
            )
            if finish_reason == "length":
                logger.warning(
                    "description_enrichment: batch response truncated; "
                    "incomplete entries are skipped."
                )
            data = yaml.safe_load(response)
            if not isinstance(data, dict):
                logger.warning(
                    "description_enrichment: batch response was not a YAML "
                    "mapping; batch skipped."
                )
                continue
            for i, entry in enumerate(batch, 1):
                desc = data.get(i, data.get(str(i)))
                if not isinstance(desc, str) or not desc.strip():
                    continue
                if _write_description(entry["md_path"], desc.strip()):
                    modified += 1
        except Exception as e:
            logger.warning(f"description_enrichment: batch failed: {e}")

    return modified
