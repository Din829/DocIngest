"""
Structure tests for the local-OCR backend dispatch — verifies the wiring
WITHOUT a GPU or the real model (torch/transformers not required).

What this pins:
  1. ocr_backend unset  → describe_image stays on the cloud (litellm) path.
  2. ocr_backend set     → describe_image routes to the local backend.
  3. The local seam touches ONLY OCR: text_completion / summary path is never
     diverted (it doesn't even consult ocr_backend).
  4. Output adapter strips Unlimited-OCR's <|det|> layout tokens to clean text.
  5. Missing torch/transformers fails loud with an install hint.

Run:
    python tests/unit/test_local_ocr_dispatch.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.models import local_ocr
from docingest.models import provider


def test_is_enabled_only_when_backend_set():
    print("=== test_is_enabled_only_when_backend_set ===")
    assert local_ocr.is_local_ocr_enabled(None) is False
    assert local_ocr.is_local_ocr_enabled({}) is False
    assert local_ocr.is_local_ocr_enabled({"ocr_backend": None}) is False
    assert local_ocr.is_local_ocr_enabled({"ocr_backend": "unlimited_ocr"}) is True
    print("  enabled iff ocr_backend is truthy  PASSED\n")


def test_describe_image_routes_to_local_when_enabled(tmp_img):
    """ocr_backend set → describe_image calls run_local_ocr, NOT litellm."""
    print("=== test_describe_image_routes_to_local_when_enabled ===")
    cfg = {"primary": {"provider": "google", "model": "gemini"},
           "ocr_backend": "unlimited_ocr"}
    with mock.patch.object(local_ocr, "run_local_ocr",
                           return_value="LOCAL-RESULT") as m_local, \
         mock.patch("docingest.models.provider.litellm") as m_litellm:
        out = provider.describe_image(tmp_img, "prompt", cfg)
    assert out == "LOCAL-RESULT", out
    assert m_local.called, "local backend should have been used"
    assert not m_litellm.completion.called, "litellm must NOT be called for local OCR"
    print("  local backend used, litellm bypassed  PASSED\n")


def test_describe_image_stays_cloud_when_disabled(tmp_img):
    """ocr_backend unset → describe_image uses litellm, never the local path."""
    print("=== test_describe_image_stays_cloud_when_disabled ===")
    cfg = {"primary": {"provider": "google", "model": "gemini"}}  # no ocr_backend
    fake_resp = mock.Mock()
    fake_resp.choices = [mock.Mock(message=mock.Mock(content="CLOUD-RESULT"))]
    with mock.patch.object(local_ocr, "run_local_ocr") as m_local, \
         mock.patch("docingest.models.provider.litellm") as m_litellm, \
         mock.patch("docingest.models.provider._record_usage"), \
         mock.patch("docingest.models.provider._set_api_key"):
        m_litellm.completion.return_value = fake_resp
        out = provider.describe_image(tmp_img, "prompt", cfg)
    assert out == "CLOUD-RESULT", out
    assert m_litellm.completion.called, "litellm should be the cloud path"
    assert not m_local.called, "local backend must NOT fire when disabled"
    print("  cloud path used, local backend untouched  PASSED\n")


def test_text_completion_never_consults_ocr_backend():
    """The summary/chunking/refine seam (text_completion) must be independent of
    ocr_backend — a local OCR model can't do text tasks, so they stay on cloud.
    We assert text_completion's code does not even reference local_ocr."""
    print("=== test_text_completion_never_consults_ocr_backend ===")
    import inspect
    src = inspect.getsource(provider.text_completion)
    assert "local_ocr" not in src and "ocr_backend" not in src, (
        "text_completion must not route on ocr_backend — text tasks stay cloud"
    )
    print("  text_completion is independent of the local OCR seam  PASSED\n")


def test_output_adapter_strips_det_tokens():
    """Unlimited-OCR's <|det|>...<|/det|> layout wrapper is stripped to text."""
    print("=== test_output_adapter_strips_det_tokens ===")
    raw = (
        "<|det|>title [10, 20, 30, 40]<|/det|>すき家メニュー\n"
        "<|det|>text [10, 50, 30, 60]<|/det|>栄養成分について\n"
        "<table><tr><td>A</td><td>1</td></tr></table>"
    )
    md = local_ocr._unlimited_to_markdown(raw, keep_boxes=False)
    assert "<|det|>" not in md and "<|/det|>" not in md, md
    assert "すき家メニュー" in md and "栄養成分について" in md, md
    assert "[10, 20, 30, 40]" not in md, "coordinate brackets should be gone"
    assert "<table>" in md, "HTML tables pass through"
    print("  det tokens stripped, text + tables preserved  PASSED\n")


def test_missing_deps_fail_loud():
    """No torch/transformers → LocalOCRDependencyError with an install hint,
    never a bare ImportError or silent degrade."""
    print("=== test_missing_deps_fail_loud ===")
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def deny_torch(name, *a, **k):
        if name in ("torch", "transformers"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *a, **k)

    with mock.patch("builtins.__import__", side_effect=deny_torch):
        try:
            local_ocr._load_unlimited_ocr("baidu/Unlimited-OCR", "cuda")
            assert False, "should have raised LocalOCRDependencyError"
        except local_ocr.LocalOCRDependencyError as e:
            assert "local-ocr" in str(e), "error should hint the install extra"
    print("  missing deps raise LocalOCRDependencyError with install hint  PASSED\n")


def test_unknown_backend_rejected():
    print("=== test_unknown_backend_rejected ===")
    try:
        local_ocr._get_handle("does_not_exist", "x", "cuda")
        assert False, "unknown backend should raise"
    except ValueError as e:
        assert "Unknown local OCR backend" in str(e)
    print("  unknown backend name rejected loudly  PASSED\n")


def _make_tmp_img() -> Path:
    import tempfile
    p = Path(tempfile.mkdtemp()) / "page.png"
    # 1x1 PNG so Path.exists() passes; content never read on the mocked paths.
    p.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000050001"
        "0a2db40000000049454e44ae426082"
    ))
    return p


def main():
    test_is_enabled_only_when_backend_set()
    img = _make_tmp_img()
    test_describe_image_routes_to_local_when_enabled(img)
    test_describe_image_stays_cloud_when_disabled(img)
    test_text_completion_never_consults_ocr_backend()
    test_output_adapter_strips_det_tokens()
    test_missing_deps_fail_loud()
    test_unknown_backend_rejected()
    print("ALL local-OCR-dispatch TESTS PASSED")


# pytest-style fixture name used positionally by main(); kept simple.
tmp_img = property  # placeholder so module imports cleanly under pytest collectors


if __name__ == "__main__":
    main()
