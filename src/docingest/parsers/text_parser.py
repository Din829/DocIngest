"""
Text/Markdown parser — transparent pass-through.

Handles .md, .txt, .csv and other plain text files.
No conversion needed — just reads the file and wraps as ParseResult.

Also serves as FallbackParser for unknown formats:
tries multiple encodings (UTF-8 → Shift-JIS → Latin-1).
"""

from __future__ import annotations

from pathlib import Path

from .base import BaseParser, ParseResult


class TextParser(BaseParser):
    """Pass-through parser for text-based files."""

    def parse(self, file_path: Path) -> ParseResult:
        """Read file as text, try multiple encodings."""
        # Quick binary check: if file contains null bytes, it's likely binary
        try:
            raw = file_path.read_bytes()
            if b"\x00" in raw[:8192]:
                return ParseResult(
                    markdown="",
                    success=False,
                    error=f"Binary file detected (contains null bytes): {file_path.name}",
                )
        except Exception as e:
            return ParseResult(markdown="", success=False, error=f"Cannot read: {e}")

        # Try encodings in order (most common first)
        # utf-8-sig: handles BOM; cp932: Japanese Windows superset of Shift-JIS
        encodings = ["utf-8", "utf-8-sig", "cp932", "shift_jis", "euc-jp", "latin-1"]

        for enc in encodings:
            try:
                content = raw.decode(enc)
                return ParseResult(
                    markdown=content,
                    metadata={
                        "format": file_path.suffix.lstrip(".") or "txt",
                        "title": file_path.stem,
                        "encoding_detected": enc,
                    },
                    success=True,
                )
            except (UnicodeDecodeError, UnicodeError):
                continue

        # Last resort: UTF-8 with replacement (keeps most content, replaces bad bytes with �)
        content = raw.decode("utf-8", errors="replace")
        return ParseResult(
            markdown=content,
            metadata={
                "format": file_path.suffix.lstrip(".") or "txt",
                "title": file_path.stem,
                "encoding_detected": "utf-8 (lossy)",
            },
            success=True,
        )

    def supported_extensions(self) -> set[str]:
        return {".md", ".txt", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml", ".toml"}
