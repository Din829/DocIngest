"""
Native-video Files API upload timeout tests.

No real Gemini calls. These tests pin the boundary behaviour that prevents a
large-video Files API upload from hanging the whole run.

Run:
    python tests/unit/test_native_video_upload_timeout.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from docingest.config import load_config  # noqa: E402
from docingest.models import provider as provider_module  # noqa: E402
from docingest.parsers.media_parser import MediaParser  # noqa: E402
from docingest.pipeline import process_single_file  # noqa: E402


class _FakeHttpOptions:
    def __init__(self, timeout=None):
        self.timeout = timeout


class _FakeUploadFileConfig:
    def __init__(self, http_options=None):
        self.http_options = http_options


class _FakeTypes:
    HttpOptions = _FakeHttpOptions
    UploadFileConfig = _FakeUploadFileConfig


class _SdkUploadTimeout(Exception):
    """SDK-style timeout name; not built-in TimeoutError."""


def _base_config() -> dict:
    cfg = load_config()
    cfg.setdefault("models", {})["video_understanding"] = {
        "primary": {"provider": "google", "model": "gemini-test"},
        "max_response_tokens": 1024,
    }
    return cfg


def test_gemini_upload_uses_configured_timeout_milliseconds():
    print("=== test_gemini_upload_uses_configured_timeout_milliseconds ===")
    calls = {}

    class _Files:
        @staticmethod
        def upload(*, file, config=None):
            calls["file"] = file
            calls["config"] = config
            return SimpleNamespace(name="files/ok")

    client = SimpleNamespace(files=_Files())
    video = Path("movie.mp4")

    result = provider_module._upload_gemini_file(
        client=client,
        types_module=_FakeTypes,
        video_path=video,
        upload_timeout_sec=600,
    )

    assert result.name == "files/ok"
    assert calls["file"] == str(video)
    assert calls["config"].http_options.timeout == 600000
    print("  PASSED\n")


def test_gemini_upload_converts_sdk_timeout_to_builtin_timeout_error():
    print("=== test_gemini_upload_converts_sdk_timeout_to_builtin_timeout_error ===")

    class _Files:
        @staticmethod
        def upload(*, file, config=None):
            raise _SdkUploadTimeout("socket stalled")

    client = SimpleNamespace(files=_Files())

    try:
        provider_module._upload_gemini_file(
            client=client,
            types_module=_FakeTypes,
            video_path=Path("large.mp4"),
            upload_timeout_sec=600,
        )
        raise AssertionError("expected TimeoutError")
    except TimeoutError as e:
        msg = str(e)
        assert "files_api_upload_timeout_sec" in msg
        assert "large.mp4" in msg
    print("  PASSED\n")


def test_media_parser_does_not_fallback_on_native_video_timeout():
    print("=== test_media_parser_does_not_fallback_on_native_video_timeout ===")
    cfg = _base_config()
    parser = MediaParser(cfg)

    with mock.patch.object(
        provider_module,
        "describe_video",
        side_effect=TimeoutError("upload timed out"),
    ):
        try:
            parser._parse_via_native_video(Path("large.mp4"))
            raise AssertionError("expected TimeoutError")
        except TimeoutError:
            pass
    print("  PASSED\n")


def test_describe_video_does_not_wrap_upload_timeout_as_runtime_error():
    print("=== test_describe_video_does_not_wrap_upload_timeout_as_runtime_error ===")
    cfg = {
        "primary": {"provider": "google", "model": "gemini-test"},
        "max_response_tokens": 1024,
    }
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "large.mp4"
        video.write_bytes(b"fake")
        with mock.patch.object(
            provider_module,
            "_describe_video_gemini",
            side_effect=TimeoutError("upload timed out"),
        ):
            try:
                provider_module.describe_video(video, "prompt", cfg)
                raise AssertionError("expected TimeoutError")
            except TimeoutError:
                pass
    print("  PASSED\n")


def test_pipeline_records_parser_timeout_error_type():
    print("=== test_pipeline_records_parser_timeout_error_type ===")

    class _TimeoutParser:
        def parse(self, file_path, override_stream=None):
            raise TimeoutError("upload timed out")

    cfg = _base_config()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "large.mp4"
        src.write_bytes(b"not a real video")
        result, chunks = process_single_file(
            src,
            _TimeoutParser(),
            chunker=None,
            config=cfg,
            output_dir=root / "out",
        )

    assert chunks == []
    assert result.success is False
    assert result.error_type == "timeout"
    assert "Parse timed out" in result.error
    print("  PASSED\n")


if __name__ == "__main__":
    test_gemini_upload_uses_configured_timeout_milliseconds()
    test_gemini_upload_converts_sdk_timeout_to_builtin_timeout_error()
    test_media_parser_does_not_fallback_on_native_video_timeout()
    test_describe_video_does_not_wrap_upload_timeout_as_runtime_error()
    test_pipeline_records_parser_timeout_error_type()
    print("=== ALL NATIVE-VIDEO UPLOAD TIMEOUT TESTS PASSED ===")
