"""
Base parser interface.

All parsers implement this interface. The pipeline calls parse() and gets
back a ParseResult containing Markdown text + metadata.

Design: Parsers are stateless — all configuration comes from the config dict
passed to __init__. This makes them easy to test and swap.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ParseResult:
    """Result of parsing a single document."""

    # The extracted content as Markdown
    markdown: str

    # Metadata about the parsed document
    metadata: dict[str, Any] = field(default_factory=dict)
    # Expected keys: format, title, language, pages, has_tables, has_images

    # Extracted images (path on disk → description if available)
    images: dict[str, str] = field(default_factory=dict)
    # key: asset output path (e.g. "assets/report-p12-chart.png")
    # value: description text (empty string if no Vision description yet)

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
    def parse(self, file_path: Path) -> ParseResult:
        """
        Parse a document file and return Markdown + metadata.

        Args:
            file_path: Absolute path to the input file.

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
