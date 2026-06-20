"""Type derivation hook (pre_write) — semantic concept type for each file.

Writes `metadata["type"]` — a short, human- and agent-readable string
describing WHAT KIND of knowledge a file is ("Document", "Spreadsheet",
"Transcript", …), distinct from `format` (the file extension / how to open
it). Agents use `type` for routing and filtering: "search only Spreadsheet
files", "skip Transcripts", etc. It also makes DocIngest output align with
the Open Knowledge Format (OKF), whose ONLY required frontmatter field is
`type` (see 参考项目/knowledge-catalog/okf/SPEC.md §4.1).

Why map from `format`, not infer from content:
  Content inference needs an LLM — slow, costly, and unreliable for a label.
  The file format already carries the type signal cheaply and deterministically
  (a .pdf is a Document, an .m4a is a Transcript). So we map format → type via
  a CONFIG-DRIVEN table (output.derived_metadata.type.mapping), keeping it
  flexible: deployments add/override mappings without touching code, and a
  format not in the table falls back to a configurable default ("Document").
  The fallback is a legitimate default, not error-hiding — every file gets a
  meaningful type, and an unrecognized format is a real "generic document",
  not a failure.

Mirrors derive_tags_hook: self-contained, config-gated, single field,
raises HookNoOp when disabled or when nothing can be written.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..parsers.base import ParseResult
from . import HookNoOp

logger = logging.getLogger(__name__)


# Built-in format → semantic type defaults. Used ONLY when config provides no
# mapping (config wins entirely when present). Values follow OKF's convention:
# short, self-explanatory, human-readable; not centrally registered. Keys are
# lowercase file formats as produced by the parsers (metadata["format"]).
_DEFAULT_TYPE_MAPPING: dict[str, str] = {
    # Documents
    "pdf": "Document",
    "docx": "Document",
    "doc": "Document",
    "html": "WebPage",
    "htm": "WebPage",
    "md": "Note",
    "txt": "Note",
    "asciidoc": "Note",
    "adoc": "Note",
    # Tabular
    "xlsx": "Spreadsheet",
    "xls": "Spreadsheet",
    "csv": "Spreadsheet",
    "tsv": "Spreadsheet",
    # Slides
    "pptx": "Presentation",
    "ppt": "Presentation",
    # Images
    "png": "Image",
    "jpg": "Image",
    "jpeg": "Image",
    "tiff": "Image",
    "bmp": "Image",
    "webp": "Image",
    "gif": "Image",
    # Audio / video → transcript (DocIngest emits a transcript for these)
    "m4a": "Transcript",
    "mp3": "Transcript",
    "wav": "Transcript",
    "flac": "Transcript",
    "aac": "Transcript",
    "ogg": "Transcript",
    "mp4": "Transcript",
    "mov": "Transcript",
    "mkv": "Transcript",
    "webm": "Transcript",
    "avi": "Transcript",
}

# Fallback type when a format is in neither the config mapping nor the
# built-in table. A generic, OKF-valid type — not an error signal.
_DEFAULT_FALLBACK_TYPE = "Document"


def derive_type_hook(
    file_path: Path,
    parse_result: ParseResult,
    config: dict[str, Any],
) -> None:
    """Set metadata["type"] from the file format via a config-driven map."""

    if not get_nested(config, "output.derived_metadata.type.enabled", True):
        raise HookNoOp

    # A parser / upstream hook may already have set an explicit type — respect
    # it (never clobber a more specific decision made closer to the source).
    if isinstance(parse_result.metadata.get("type"), str) and parse_result.metadata["type"].strip():
        raise HookNoOp

    fmt = parse_result.metadata.get("format")
    if not isinstance(fmt, str) or not fmt.strip():
        raise HookNoOp  # no format signal → leave type absent rather than guess
    fmt = fmt.strip().lower()

    # Config mapping wins entirely when present; otherwise use the built-in
    # defaults. (Config is a full replacement, not a merge — keeps the
    # resolution rule simple and predictable.)
    mapping = get_nested(config, "output.derived_metadata.type.mapping", None)
    if not isinstance(mapping, dict) or not mapping:
        mapping = _DEFAULT_TYPE_MAPPING

    fallback = get_nested(
        config, "output.derived_metadata.type.fallback", _DEFAULT_FALLBACK_TYPE
    )

    # Case-insensitive lookup against mapping keys (config authors may write
    # "PDF" or "pdf").
    resolved = None
    for k, v in mapping.items():
        if isinstance(k, str) and k.strip().lower() == fmt:
            resolved = v
            break

    parse_result.metadata["type"] = resolved if isinstance(resolved, str) and resolved.strip() else fallback
