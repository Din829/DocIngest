"""
Document parsers — Phase 1: Raw document → Markdown.

Parser routing:
  1. DoclingParser (default, handles 15+ formats)
  2. TextParser (fallback for plain text / unknown formats)

No manual extension-based routing — Docling handles format detection internally.
If Docling fails, TextParser tries to read as plain text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseParser, ParseResult
from .docling_parser import DoclingParser
from .text_parser import TextParser


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
    Composite parser: tries Docling first, falls back to TextParser.

    This is the default parser. It's not a user-facing class — users
    configure via `parsing.engine: "docling"` in YAML.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._docling = DoclingParser(config)
        self._fallback = TextParser(config)

    def parse(self, file_path: Path) -> ParseResult:
        """Try Docling, fall back to text extraction."""
        # First try Docling
        result = self._docling.parse(file_path)
        if result.success:
            return result

        # Docling failed → try plain text fallback
        fallback_result = self._fallback.parse(file_path)
        if fallback_result.success:
            # Mark that we used fallback
            fallback_result.metadata["parser_fallback"] = True
            return fallback_result

        # Both failed
        return ParseResult(
            markdown="",
            success=False,
            error=f"All parsers failed: Docling({result.error}), Text({fallback_result.error})",
        )

    def supported_extensions(self) -> set[str]:
        return self._docling.supported_extensions() | self._fallback.supported_extensions()
