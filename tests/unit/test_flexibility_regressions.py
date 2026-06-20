"""Regression tests for flexibility/fail-loud behaviour."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from docingest.config import load_config  # noqa: E402
from docingest.incremental import compute_config_hash  # noqa: E402
from docingest.parsers import _DoclingWithFallback  # noqa: E402
from docingest.parsers.base import ParseResult  # noqa: E402
from docingest.parsers.text_parser import TextParser  # noqa: E402


def _set_path(data: dict, path: str, value: object) -> None:
    cur = data
    parts = path.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


class FlexibilityRegressionTests(unittest.TestCase):
    def test_native_video_output_config_invalidates_incremental_hash(self) -> None:
        base_config = load_config()
        base_hash = compute_config_hash(base_config)

        cases = [
            ("parsing.audio.native_video.enabled", False),
            ("parsing.audio.native_video.fps", 0.5),
            ("models.video_understanding.primary.provider", "google"),
            ("models.video_understanding.primary.model", "gemini-test-video-model"),
            ("models.video_understanding.max_response_tokens", 4096),
        ]
        for path, value in cases:
            cfg = copy.deepcopy(base_config)
            _set_path(cfg, path, value)
            self.assertNotEqual(compute_config_hash(cfg), base_hash, path)

        # Runtime boundary knobs affect reliability, not markdown content;
        # changing them should not invalidate successful cached outputs.
        runtime_cases = [
            ("parsing.audio.native_video.files_api_upload_timeout_sec", 123),
            ("parsing.audio.native_video.files_api_poll_timeout_sec", 456),
        ]
        for path, value in runtime_cases:
            cfg = copy.deepcopy(base_config)
            _set_path(cfg, path, value)
            self.assertEqual(compute_config_hash(cfg), base_hash, path)

    def test_media_failure_does_not_text_fallback_to_garbage_success(self) -> None:
        class FakeMedia:
            def accepts(self, file_path: Path) -> bool:
                return file_path.suffix.lower() == ".mp4"

            def parse(self, file_path: Path) -> ParseResult:
                return ParseResult(markdown="", success=False, error="media boom")

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "bad.mp4"
            video.write_bytes(bytes([1, 2, 3, 4, 255, 254, 253]) + b"not real video")

            parser = object.__new__(_DoclingWithFallback)
            parser.config = {}
            parser._media = FakeMedia()
            parser._fallback = TextParser({})
            parser._get_media_parser = lambda: parser._media

            result = parser.parse(video)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "media boom")
        self.assertIsNone(result.metadata.get("parser_fallback"))

    def test_binary_format_failure_does_not_text_fallback_to_garbage_success(self) -> None:
        """A failed Docling parse on a BINARY format (PDF/Office/image) must NOT
        fall through to TextParser — that would decode the raw bytes as text and
        report a bogus "success" full of garbage (e.g. a corrupt PDF's
        `%PDF-1.4 …%%EOF` read as document content), silently polluting the KB.
        Mirrors the MediaParser guard above. Plain-text extensions are exempt
        (covered by the separate case below)."""
        class FakeDocling:
            def parse(self, file_path: Path, *, override_stream=None) -> ParseResult:
                return ParseResult(markdown="", success=False, error="docling boom")

        with tempfile.TemporaryDirectory() as tmp:
            # A "PDF" that is really plain-text garbage — TextParser COULD decode
            # it (no null bytes), which is exactly the bogus-success trap.
            pdf = Path(tmp) / "corrupt.pdf"
            pdf.write_text("%PDF-1.4\nnot a real pdf\n%%EOF", encoding="utf-8")

            parser = object.__new__(_DoclingWithFallback)
            parser.config = {}
            parser._docling = FakeDocling()
            parser._fallback = TextParser({})
            parser._media = None
            parser._get_media_parser = lambda: None

            result = parser.parse(pdf)

        # Guard fired: Docling's failure is returned as-is, NOT a TextParser
        # success. No parser_fallback, no garbage markdown.
        self.assertFalse(result.success)
        self.assertEqual(result.error, "docling boom")
        self.assertIsNone(result.metadata.get("parser_fallback"))

    def test_text_format_failure_still_falls_back_to_textparser(self) -> None:
        """Counterpart to the guard: a NON-binary extension (.txt) whose Docling
        parse fails MUST still fall through to TextParser — the guard only
        covers binary formats, so legitimate text fallback is unaffected."""
        class FakeDocling:
            def parse(self, file_path: Path, *, override_stream=None) -> ParseResult:
                return ParseResult(markdown="", success=False, error="docling boom")

        with tempfile.TemporaryDirectory() as tmp:
            txt = Path(tmp) / "notes.txt"
            txt.write_text("real text content", encoding="utf-8")

            parser = object.__new__(_DoclingWithFallback)
            parser.config = {}
            parser._docling = FakeDocling()
            parser._fallback = TextParser({})
            parser._media = None
            parser._get_media_parser = lambda: None

            result = parser.parse(txt)

        # .txt is not a binary format → TextParser fallback fires as before.
        self.assertTrue(result.success)
        self.assertTrue(result.metadata.get("parser_fallback"))
        self.assertIn("real text content", result.markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
