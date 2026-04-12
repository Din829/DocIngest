"""
Base parser interface.

All parsers implement this interface. The pipeline calls parse() and gets
back a ParseResult containing Markdown text + metadata.

Design: Parsers are stateless — all configuration comes from the config dict
passed to __init__. This makes them easy to test and swap.
"""

from __future__ import annotations

# Shared marker used by Docling export and pipeline page splitting
PAGEBREAK_MARKER = "<!-- pagebreak -->"

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any


@dataclass
class PageData:
    """Per-page data extracted by parser, for Vision enrichment."""
    page_no: int
    text: str              # Docling-extracted text for this page
    image_path: str = ""   # Path to saved page image (for Vision)


@dataclass
class ParseResult:
    """Result of parsing a single document."""

    # The extracted content as Markdown
    markdown: str

    # Metadata about the parsed document
    metadata: dict[str, Any] = field(default_factory=dict)
    # Expected keys: format, title, language, pages, has_tables, has_images

    # Per-page data (page text + image path) for Vision enrichment
    pages: list[PageData] = field(default_factory=list)

    # Legacy: kept for backward compatibility with TextParser
    images: dict[str, str] = field(default_factory=dict)

    # Whether parsing succeeded (False = fallback was used or partial result)
    success: bool = True

    # Error message if success is False
    error: str = ""


class BaseParser(ABC):
    """
    Abstract base class for document parsers.

    Subclasses must implement:
      - parse(file_path) → ParseResult
      - supported_extensions() → set of extensions this parser handles

    Lifecycle:
      1. __init__(config) — receive full config dict
      2. parse(file_path) — called once per file
      3. (no cleanup needed — parsers are stateless)
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    def parse(
        self,
        file_path: Path,
        *,
        override_stream: BytesIO | None = None,
    ) -> ParseResult:
        """
        Parse a document file and return Markdown + metadata.

        Args:
            file_path: Absolute path to the input file. Even when
                override_stream is provided, file_path is still used for
                naming, format detection, and metadata.
            override_stream: Optional BytesIO with transformed file content
                (e.g. from a pre-parse hook). When provided, implementations
                should read from the stream instead of file_path. Default
                None means "read the file on disk as usual".

        Returns:
            ParseResult with markdown content and metadata.

        Raises:
            Should NOT raise exceptions — return ParseResult(success=False)
            with error message instead. Let the pipeline handle errors.
        """
        ...

    @abstractmethod
    def supported_extensions(self) -> set[str]:
        """
        Return the set of file extensions this parser can handle.

        Example: {".pdf", ".pptx", ".xlsx", ".html", ".png", ".jpg"}

        Used by the pipeline to verify parser capability, NOT for routing
        (Docling handles its own routing internally).
        """
        ...
