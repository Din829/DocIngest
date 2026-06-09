"""
Markdown writer — writes parsed documents to sources/ with YAML frontmatter.

Takes a ParseResult (in-memory Markdown + metadata) and writes it to disk
as a properly formatted Markdown file with frontmatter header.

Design:
  - Frontmatter is optional (config: output.markdown.include_metadata_header)
  - Large files can be split (config: output.markdown.max_file_size_mb)
  - Output filename derived from original file stem
  - Duplicate filenames get numeric suffix (_1, _2, etc.)
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..parsers.base import ParseResult


# Match a line that begins with `- **label**:` — the historical native_video
# output shape that downstream chunkers misread as a Markdown list. The label
# text is intentionally not enumerated: model wording drifts, but the shape
# stays stable.
_VIDEO_BOLD_LABEL = r"\*\*[^*\n]{1,40}\*\*:"
_VIDEO_FAKE_LIST_RE = re.compile(
    rf"^- ({_VIDEO_BOLD_LABEL})",
    re.MULTILINE,
)
_VIDEO_LABEL_PARAGRAPH_RE = re.compile(
    rf"(?<!\n)\n({_VIDEO_BOLD_LABEL})",
)


def _normalize_video_markdown(markdown: str) -> str:
    """Strip the leading `- ` from video `- **label**: ...` lines
    and ensure each label sits in its own paragraph (blank line before).

    Why this exists: the chunkers' list-protection rule treats any `- ` line
    as part of a list block and refuses to split it, so a 4000-token
    speech+visual segment becomes one chunk. Plain paragraphs let the
    recursive chunker split by `\\n\\n`. Idempotent — running it on
    already-normalized text is a no-op.
    """
    if "- **" not in markdown:
        return markdown
    # 1) drop the leading "- " before bold labels
    fixed = _VIDEO_FAKE_LIST_RE.sub(r"\1", markdown)
    # 2) guarantee a blank line before each label so it stands alone as a
    #    paragraph (chunker's paragraph boundary is "\n\n"). The label may
    #    already have one — we collapse "\n*\n**X**" patterns to one form.
    fixed = _VIDEO_LABEL_PARAGRAPH_RE.sub(r"\n\n\1", fixed)
    return fixed


def _yaml_escape(value: Any) -> str:
    """
    Minimal YAML scalar escape: wrap in double quotes when the value
    contains characters that could confuse a YAML parser (colon, newline,
    leading/trailing whitespace). Numbers and bools pass through.
    """
    if isinstance(value, (int, float, bool)):
        return str(value)
    s = str(value)
    if not s:
        return '""'
    needs_quote = (
        ":" in s or "\n" in s or "#" in s
        or s != s.strip()
        or s[0] in "[]{}|>*&!%@`"
    )
    if needs_quote:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        return f'"{escaped}"'
    return s


# Default frontmatter field order used when config doesn't override
# output.markdown.frontmatter_fields. Explicit high-value fields first (for
# readability), then optional enrichment fields added by hooks. Unknown
# fields in metadata are NOT auto-exported — only listed fields make it to
# frontmatter. Hooks that add new metadata keys must either extend this
# list via config or rely on the nested exif: block below for overflow.
_DEFAULT_FRONTMATTER_FIELDS: list[str] = [
    # Core identity (always present)
    "format",
    "title",
    "language",
    "pages",
    # Docling origin (promoted by file_metadata hook)
    "mimetype",
    "binary_hash",
    # Exiftool enrichment (populated when metadata.exiftool.enabled)
    "author",
    "created_at",
]


def _build_frontmatter(
    metadata: dict[str, Any],
    original_file: str,
    config: dict[str, Any] | None = None,
) -> str:
    """
    Build YAML frontmatter string from metadata.

    Field selection and order come from config
    (output.markdown.frontmatter_fields); when absent, fall back to
    _DEFAULT_FRONTMATTER_FIELDS. Missing fields are skipped silently, so
    hooks can opportunistically write data without worrying about whether
    every file carries every field. Additional hook-provided metadata
    (e.g. the exif dict) is attached as a nested block at the end.
    """
    lines = ["---"]
    lines.append(f"source: {_yaml_escape(original_file)}")

    # Resolve field list: config > default. Config value must be a list
    # of strings; anything else falls back to the default to avoid silent
    # misconfiguration breaking frontmatter output.
    fields: list[str] = _DEFAULT_FRONTMATTER_FIELDS
    if config is not None:
        configured = get_nested(config, "output.markdown.frontmatter_fields", None)
        if isinstance(configured, list) and all(isinstance(f, str) for f in configured):
            fields = configured

    for field in fields:
        if field not in metadata or metadata[field] is None:
            continue
        value = metadata[field]
        # Lists render as block-style YAML so Obsidian / Bases / generic
        # YAML parsers see them as proper lists (not stringified Python
        # repr). Empty lists are skipped — an empty `tags:` line is
        # harmless but noisy.
        if isinstance(value, list):
            if not value:
                continue
            lines.append(f"{field}:")
            for item in value:
                lines.append(f"  - {_yaml_escape(item)}")
        else:
            lines.append(f"{field}: {_yaml_escape(value)}")

    lines.append(f"processed_at: {datetime.datetime.now().isoformat(timespec='seconds')}")

    # Surface warnings to frontmatter so both humans and Agents can see them
    if "warnings" in metadata and metadata["warnings"]:
        for w in metadata["warnings"]:
            lines.append(f"warning: {_yaml_escape(w)}")

    # Nested exif block (from file_metadata hook when exiftool is enabled).
    # Only emitted when non-empty so noise is minimized.
    exif = metadata.get("exif")
    if isinstance(exif, dict) and exif:
        lines.append("exif:")
        for k, v in exif.items():
            lines.append(f"  {k}: {_yaml_escape(v)}")

    lines.append("---")
    return "\n".join(lines)


def _resolve_output_path(sources_dir: Path, stem: str, existing: set[str]) -> Path:
    """
    Resolve output file path, avoiding duplicates.

    If "report.md" already exists, returns "report_1.md", "report_2.md", etc.
    """
    name = f"{stem}.md"
    if name not in existing:
        existing.add(name)
        return sources_dir / name

    # Find next available suffix
    counter = 1
    while True:
        name = f"{stem}_{counter}.md"
        if name not in existing:
            existing.add(name)
            return sources_dir / name
        counter += 1


def write_markdown(
    parse_result: ParseResult,
    original_file: Path,
    output_dir: Path,
    config: dict[str, Any],
    existing_names: set[str] | None = None,
) -> Path:
    """
    Write a ParseResult to a Markdown file in sources/.

    Args:
        parse_result: Parsed document (markdown + metadata).
        original_file: Original input file path.
        output_dir: Base output directory (e.g. ./knowledge/).
        config: Full config dict.
        existing_names: Set of already-used output filenames (for dedup).
            Mutated in-place when a new name is added.

    Returns:
        Path to the written .md file (relative to output_dir).
    """
    if existing_names is None:
        existing_names = set()

    sources_dir = output_dir / get_nested(config, "output.sources_dir", "sources")
    sources_dir.mkdir(parents=True, exist_ok=True)

    # Resolve output path (handle duplicates)
    output_path = _resolve_output_path(sources_dir, original_file.stem, existing_names)

    # Build content
    parts: list[str] = []

    # Frontmatter (optional)
    include_header = get_nested(config, "output.markdown.include_metadata_header", True)
    if include_header:
        frontmatter = _build_frontmatter(
            parse_result.metadata, original_file.name, config=config
        )
        parts.append(frontmatter)
        parts.append("")  # blank line after frontmatter

    # Main content. For video (native or frame-sampling) the prompt asks for
    # plain paragraphs, but model output can drift back to `- **说**:` list
    # form; normalize so the chunker's list-protection rule never glues a
    # whole multi-thousand-token segment into one chunk. Skipped for non-video:
    # chart/table descriptions can legitimately use `- **Y-axis**:`-style
    # lists, and those should stay lists.
    body = parse_result.markdown
    if (parse_result.metadata.get("format") or "").lower() in {
        "mp4", "avi", "mkv", "webm", "mov", "wmv", "flv", "ts", "m4v"
    }:
        body = _normalize_video_markdown(body)
    parts.append(body)

    content = "\n".join(parts)

    # Write
    output_path.write_text(content, encoding="utf-8")

    return output_path
