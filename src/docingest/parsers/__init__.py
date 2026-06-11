"""
Document parsers — Phase 1: Raw document → Markdown.

Parser routing (priority order):
  1. MediaParser (audio/video files — subtitle-first + ASR fallback)
  2. DoclingParser (default, handles 15+ document formats)
  3. TextParser (fallback for plain text / unknown formats)

MediaParser is checked first because Docling cannot handle audio/video.
The check is cheap (extension match), so non-media files skip it instantly.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import Any

from ..config import get_nested
from .base import BaseParser, ParseResult
from .docling_parser import DoclingParser
from .text_parser import TextParser

logger = logging.getLogger(__name__)


# Inputs handled by pipeline stages BEFORE any parser sees them, so no
# parser's supported_extensions() reports them. Each is gated by its own
# config switch — turn the feature off and the extension leaves the set.
#   .xls/.doc/.ppt → Phase 0.5 LibreOffice conversion to OOXML
#   .zip           → discover_files expansion
_PIPELINE_LEVEL_EXTENSIONS: list[tuple[str, str]] = [
    (".xls", "parsing.xls.auto_convert_to_xlsx"),
    (".doc", "parsing.doc.auto_convert_to_docx"),
    (".ppt", "parsing.ppt.auto_convert_to_pptx"),
    (".zip", "parsing.zip.enabled"),
]


def supported_input_extensions(config: dict[str, Any]) -> set[str]:
    """Every file extension the pipeline can process under this config.

    The single source of truth for "what can DocIngest take as input" —
    entry-point filters (GUI file dialog / drag-drop) should call this
    instead of hardcoding a list. Composed of:

      * the active parser's supported_extensions() (Docling ∪ Text ∪ Media;
        media formats come from config and disappear automatically when the
        audio extras aren't installed), plus
      * pipeline-level inputs (legacy Office, ZIP), each present only while
        its config switch is on.

    Directories and URLs are separate input channels, not extensions, so
    they are out of scope here.
    """
    exts = set(create_parser(config).supported_extensions())
    for ext, cfg_path in _PIPELINE_LEVEL_EXTENSIONS:
        if get_nested(config, cfg_path, True):
            exts.add(ext)
    return exts


def create_parser(config: dict[str, Any]) -> BaseParser:
    """
    Create the appropriate parser based on config.

    The returned parser wraps Docling with TextParser as fallback.

    Args:
        config: Full config dict.

    Returns:
        Parser instance ready to use.
    """
    engine = config.get("parsing", {}).get("engine", "docling")

    if engine in ("docling", "docling_with_fallback"):
        return _DoclingWithFallback(config)
    else:
        # Unknown engine → just use text parser
        return TextParser(config)


class _DoclingWithFallback(BaseParser):
    """
    Composite parser: routes to the best parser for each file.

    Priority:
      1. MediaParser — handles audio/video (Docling can't).
         Check is cheap (extension match), non-media files skip instantly.
      2. DoclingParser — 15+ document formats.
      3. TextParser — plain text / unknown formats.

    This is the default parser. Users configure via
    `parsing.engine: "docling"` in YAML.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._docling = DoclingParser(config)
        self._fallback = TextParser(config)

        # MediaParser is lazily loaded to keep non-media pipelines fast
        # and to avoid hard-coupling to audio dependencies.
        self._media: Any = None  # Actually MediaParser, typed as Any for lazy load
        self._media_init_attempted = False

    def _get_media_parser(self) -> Any:
        """Lazy-init MediaParser. Returns None if unavailable."""
        if self._media_init_attempted:
            return self._media
        self._media_init_attempted = True
        try:
            from .media_parser import MediaParser
            self._media = MediaParser(self.config)
        except Exception as e:
            logger.debug(f"MediaParser not available: {e}")
        return self._media

    def parse(
        self,
        file_path: Path,
        *,
        override_stream: BytesIO | None = None,
    ) -> ParseResult:
        """Route to the best parser for this file type."""

        # Priority 1: MediaParser for audio/video files.
        # The accepts() check is a simple extension match — O(1), no I/O.
        media = self._get_media_parser()
        if media is not None and media.accepts(file_path):
            result = media.parse(file_path)
            if result.success:
                return result
            # MediaParser failed (e.g. ASR API down) — don't fall through
            # to Docling or TextParser for audio/video files. Text fallback can
            # decode arbitrary binary bytes (e.g. latin-1) and turn a real media
            # failure into a bogus success.
            return result

        # Priority 2: Docling (forwards override_stream when a pre-parse
        # hook produced a replacement stream, e.g. DOCX OMML preprocessing).
        result = self._docling.parse(file_path, override_stream=override_stream)
        if result.success:
            return result

        # Priority 3: TextParser fallback. Reads raw bytes from disk, so
        # override_stream (which targets Docling) is NOT forwarded.
        fallback_result = self._fallback.parse(file_path)
        if fallback_result.success:
            fallback_result.metadata["parser_fallback"] = True
            return fallback_result

        # All failed
        return ParseResult(
            markdown="",
            success=False,
            error=f"All parsers failed: Docling({result.error}), Text({fallback_result.error})",
        )

    def supported_extensions(self) -> set[str]:
        exts = self._docling.supported_extensions() | self._fallback.supported_extensions()
        media = self._get_media_parser()
        if media is not None:
            exts |= media.supported_extensions()
        return exts
