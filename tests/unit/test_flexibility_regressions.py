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


if __name__ == "__main__":
    unittest.main(verbosity=2)
