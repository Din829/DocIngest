"""
Tags enrichment — stage 2 of the tags derivation flow.

Stage 1 (`hooks/derive_tags.py`) writes `tags: [format/<f>, lang/<l>]`
into each sources/*.md frontmatter at write-time. Stage 2 (this file)
runs AFTER the pipeline finishes its main loop and the knowledge_map
has been built, then appends discriminative keyword tags
(`kw/<keyword>`) to each file's frontmatter in place.

Why a separate pass:
  - Keyword discrimination (TF-IDF document-frequency cut-off) is a
    corpus-level signal — a keyword's "good tag" rating depends on how
    many OTHER files contain it. That data only exists after every file
    has been parsed.
  - knowledge_map.py already does the discrimination filter
    (`max_doc_frequency_ratio`); stage 2 just maps that filtered
    keyword_index back to per-file frontmatter.

Design notes:
  - Idempotent: re-running on an already-enriched file does not add
    duplicate tags (kw/X is matched case-insensitively against existing
    entries before append).
  - Frontmatter-only: rewrites the leading `---\n...\n---\n` block. The
    document body is byte-for-byte preserved.
  - Best-effort: any failure (missing file, parse error) is logged at
    debug level and the file is left untouched. Pipeline continues.
  - kw/<word> namespace keeps these distinct from format/<f> and
    lang/<l>, so users can collapse / filter them separately in
    Obsidian or grep them apart in shell.
"""

from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from ..config import get_nested

logger = logging.getLogger(__name__)


# Match a leading YAML frontmatter block. Must start at file head.
# Captures the body so we can parse, mutate, and re-serialize.
_FRONTMATTER_RE = re.compile(
    r"\A---\n(.*?)\n---\n",
    re.DOTALL,
)


# Characters disallowed in a clean Obsidian-friendly tag. We don't try to
# transliterate non-ASCII (CJK is fine in Obsidian tags) — only collapse
# whitespace and strip a small set of punctuation that tools dislike.
_TAG_CLEAN_RE = re.compile(r"[\s,;]+")


def _slugify_keyword(kw: str) -> str | None:
    """
    Turn a raw keyword into a tag suffix.

    Returns None if the keyword would produce something useless (empty,
    pure digits, single character).
    """
    s = _TAG_CLEAN_RE.sub("_", str(kw).strip())
    s = s.strip("_")
    if not s or s.isdigit() or len(s) < 2:
        return None
    return s


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], int] | None:
    """
    Parse leading frontmatter and return (data, end_offset).

    end_offset is the index where the body starts (after the closing
    `---\\n`). Returns None when there's no frontmatter or it fails to
    parse — caller should leave the file alone in that case.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data, m.end()


def _restore_datetime_strings(data: dict[str, Any]) -> dict[str, Any]:
    """
    PyYAML's safe_load auto-parses ISO timestamps into datetime objects.
    safe_dump then re-emits them in YAML's "2026-04-28 21:43:01" form,
    losing the leading 'T' that makes the value a strict ISO 8601 string.
    Restore datetimes to their isoformat() representation so the round
    trip is byte-stable for date fields like `created` / `processed_at`.
    """
    fixed: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, datetime.datetime):
            fixed[k] = v.isoformat(timespec="seconds")
        elif isinstance(v, datetime.date):
            fixed[k] = v.isoformat()
        else:
            fixed[k] = v
    return fixed


def _serialize_frontmatter(data: dict[str, Any]) -> str:
    """Serialize a frontmatter dict back to the wrapped YAML form."""
    body = yaml.safe_dump(
        _restore_datetime_strings(data),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=120,
    ).rstrip()
    return f"---\n{body}\n---\n"


def _existing_tag_keys(tags: list[Any]) -> set[str]:
    """Lowercased set for dedup check against incoming kw/<x> additions."""
    keys: set[str] = set()
    for t in tags:
        if isinstance(t, str) and t.strip():
            keys.add(t.strip().lower())
    return keys


def enrich_file_with_keyword_tags(
    md_path: Path,
    keyword_tags: list[str],
) -> bool:
    """
    Add `kw/<word>` tags to the frontmatter of one sources/*.md file.

    Returns True iff the file was modified. Skipping (no frontmatter,
    parse error, no new tags) returns False without raising.
    """
    if not keyword_tags:
        return False

    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.debug(f"tags_enrichment: cannot read {md_path}: {e}")
        return False

    parsed = _parse_frontmatter(text)
    if parsed is None:
        return False  # no frontmatter or unparseable — leave alone
    data, body_offset = parsed

    # Existing tags can be a list, a YAML scalar (rare), or absent.
    existing = data.get("tags")
    if isinstance(existing, list):
        tags_list = list(existing)
    elif existing is None:
        tags_list = []
    else:
        # Somebody wrote a scalar — preserve as-is rather than corrupt it.
        return False

    have = _existing_tag_keys(tags_list)
    added = 0
    for raw in keyword_tags:
        slug = _slugify_keyword(raw)
        if not slug:
            continue
        candidate = f"kw/{slug}"
        if candidate.lower() in have:
            continue
        tags_list.append(candidate)
        have.add(candidate.lower())
        added += 1

    if added == 0:
        return False

    data["tags"] = tags_list
    new_text = _serialize_frontmatter(data) + text[body_offset:]
    try:
        md_path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        logger.debug(f"tags_enrichment: cannot write {md_path}: {e}")
        return False
    return True


def enrich_sources_with_tags(
    knowledge_map: dict[str, Any],
    output_dir: Path,
    config: dict[str, Any],
) -> int:
    """
    For every file in the knowledge map, append `kw/<keyword>` tags to
    its frontmatter based on the discriminative keywords already
    extracted into `knowledge_map["files"][i]["keywords"]`.

    Returns the number of files modified.
    """
    if not get_nested(
        config, "output.derived_metadata.tags.enrich_from_knowledge_map", True
    ):
        return 0
    if not get_nested(config, "output.derived_metadata.tags.enabled", True):
        return 0

    max_kw = int(
        get_nested(
            config, "output.derived_metadata.tags.max_keyword_tags", 5
        )
    )
    if max_kw <= 0:
        return 0

    # Build keyword_index → set(keyword) for fast cross-file
    # discrimination check (only keywords that survived the
    # max_doc_frequency_ratio filter end up here).
    discriminative = set(knowledge_map.get("keyword_index", {}).keys())

    modified = 0
    for f in knowledge_map.get("files", []):
        rel_path = f.get("path", "")
        if not rel_path:
            continue
        md_path = output_dir / rel_path
        if not md_path.exists():
            continue

        # Per-file keywords are already in priority order (most frequent
        # within this file first). Filter to those that also appear in
        # the discriminative set, take the top N.
        per_file = [
            kw for kw in (f.get("keywords") or [])
            if kw in discriminative
        ]
        # Single-file corpora: discriminative may filter out everything.
        # Fall back to the raw top keywords so the field isn't empty.
        if not per_file:
            per_file = list(f.get("keywords") or [])
        candidates = per_file[:max_kw]

        if enrich_file_with_keyword_tags(md_path, candidates):
            modified += 1

    return modified
