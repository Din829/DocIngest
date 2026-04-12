"""
Text/Markdown parser — transparent pass-through.

Handles .md, .txt, .csv and other plain text files.
No conversion needed — just reads the file and wraps as ParseResult.

Also serves as FallbackParser for unknown formats:
tries multiple encodings (UTF-8 → Shift-JIS → Latin-1).
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from .base import BaseParser, ParseResult


def _resolve_format(file_path: Path, config: dict[str, Any]) -> tuple[str, str | None]:
    """
    Decide the `format` metadata field for a text file.

    Returns (format, suffix_format_if_overridden). When magika overrides
    the suffix, the original is returned alongside so downstream code
    can expose it for debugging. Handles the common "no extension"
    case by letting magika fill in or falling back to "txt".
    """
    suffix = file_path.suffix.lstrip(".").lower()
    try:
        from ..utils.format_detector import detect_format
        corrected = detect_format(file_path, config)
    except Exception:
        corrected = None

    if corrected and corrected != suffix:
        return corrected, suffix

    # No magika correction; use suffix or fall back to "txt" for
    # extension-less text files.
    return (suffix or "txt"), None


class TextParser(BaseParser):
    """Pass-through parser for text-based files."""

    def parse(
        self,
        file_path: Path,
        *,
        override_stream: BytesIO | None = None,
    ) -> ParseResult:
        """Read file as text, try multiple encodings.

        override_stream is accepted for interface compatibility with
        BaseParser but is ignored — TextParser reads raw bytes from disk
        (pre-parse hooks target Docling-only transformations like OMML).
        """
        _ = override_stream  # explicitly unused
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

        # Resolve the format once (may call magika for weak extensions).
        resolved_format, overridden_suffix = _resolve_format(file_path, self.config)

        def _build_metadata(encoding_label: str) -> dict[str, Any]:
            md: dict[str, Any] = {
                "format": resolved_format,
                "title": file_path.stem,
                "encoding_detected": encoding_label,
            }
            if overridden_suffix is not None:
                md["suffix_format"] = overridden_suffix
            return md

        # Try encodings in order (most common first)
        # utf-8-sig: handles BOM; cp932: Japanese Windows superset of Shift-JIS
        encodings = ["utf-8", "utf-8-sig", "cp932", "shift_jis", "euc-jp", "latin-1"]

        for enc in encodings:
            try:
                content = raw.decode(enc)
                return ParseResult(
                    markdown=content,
                    metadata=_build_metadata(enc),
                    success=True,
                )
            except (UnicodeDecodeError, UnicodeError):
                continue

        # Last resort: UTF-8 with replacement (keeps most content, replaces bad bytes with �)
        content = raw.decode("utf-8", errors="replace")
        return ParseResult(
            markdown=content,
            metadata=_build_metadata("utf-8 (lossy)"),
            success=True,
        )

    def supported_extensions(self) -> set[str]:
        return {".md", ".txt", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml", ".toml"}
