"""
Test network-level retry plumbing — verify that litellm.completion /
litellm.transcription receive num_retries kwarg derived from config.

No real LLM calls. Uses unittest.mock to intercept litellm and assert
the kwarg is present with the expected value.

Run:
    python tests/unit/test_retry.py
"""

from __future__ import annotations

import sys
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.config import load_config
from docingest.models.provider import (
    resolve_max_retries,
    _HARD_FALLBACK_MAX_RETRIES,
)


# ---------------------------------------------------------------------------
# Pure helper unit tests — exercise resolve_max_retries precedence
# ---------------------------------------------------------------------------

def test_resolve_max_retries_no_config_uses_hardcoded_fallback():
    """When model_config is None entirely, fall back to _HARD_FALLBACK_MAX_RETRIES."""
    print("=== test_resolve_max_retries_no_config_uses_hardcoded_fallback ===")
    assert resolve_max_retries(None) == _HARD_FALLBACK_MAX_RETRIES
    assert resolve_max_retries({}) == _HARD_FALLBACK_MAX_RETRIES
    print("  PASSED\n")


def test_resolve_max_retries_reads_task_level_override():
    """Per-task max_retries takes precedence over inherited defaults."""
    print("=== test_resolve_max_retries_reads_task_level_override ===")
    cfg = {
        "max_retries": 5,
        "_defaults": {"max_retries": 2},
    }
    assert resolve_max_retries(cfg) == 5
    print("  PASSED\n")


def test_resolve_max_retries_falls_through_to_defaults():
    """Task without explicit max_retries inherits _defaults.max_retries."""
    print("=== test_resolve_max_retries_falls_through_to_defaults ===")
    cfg = {
        "_defaults": {"max_retries": 7},
    }
    assert resolve_max_retries(cfg) == 7
    print("  PASSED\n")


def test_resolve_max_retries_zero_is_honoured():
    """
    max_retries=0 is a valid value (disables retries) and must NOT be
    swallowed by a truthy check. Pins that we use `is not None`, not
    implicit bool coercion.
    """
    print("=== test_resolve_max_retries_zero_is_honoured ===")
    cfg = {"max_retries": 0, "_defaults": {"max_retries": 10}}
    assert resolve_max_retries(cfg) == 0

    cfg2 = {"_defaults": {"max_retries": 0}}
    assert resolve_max_retries(cfg2) == 0
    print("  PASSED\n")


# ---------------------------------------------------------------------------
# Default config carries max_retries — sanity check on YAML
# ---------------------------------------------------------------------------

def test_default_yaml_injects_max_retries_into_every_task():
    """
    Loading the bundled default.yaml and letting _inject_model_defaults run
    must put max_retries=2 into every models.<task>._defaults, so any caller
    reading resolve_max_retries(config["models"]["vision"]) sees it.
    """
    print("=== test_default_yaml_injects_max_retries_into_every_task ===")
    cfg = load_config()
    assert cfg["models"]["defaults"]["max_retries"] == 2

    for task_name in ("vision", "chunking_assist", "audio_transcription"):
        task_cfg = cfg["models"].get(task_name)
        if not task_cfg:
            continue  # task isn't always present
        assert task_cfg["_defaults"]["max_retries"] == 2, (
            f"task {task_name} missing max_retries in _defaults"
        )
        # resolve should return 2 for an un-overridden task
        assert resolve_max_retries(task_cfg) == 2
    print("  PASSED\n")


# ---------------------------------------------------------------------------
# Integration — mock litellm and verify num_retries reaches it
# ---------------------------------------------------------------------------

def test_describe_image_passes_num_retries_to_litellm():
    """
    describe_image() must call litellm.completion with num_retries
    derived from model_config. We mock litellm.completion, then
    assert the kwarg was present with the right value.
    """
    print("=== test_describe_image_passes_num_retries_to_litellm ===")
    from docingest.models import provider as provider_module

    # Need an on-disk image path for describe_image to accept the file.
    # Use a tiny existing fixture if available, otherwise skip gracefully.
    fixture_candidates = [
        Path(__file__).resolve().parent.parent / "fixtures" / "test_chart.pptx",
        # Even non-image files work for the kwarg-plumbing test — describe_image
        # only checks that the path exists before reading bytes. Skipping
        # decode errors is fine because we short-circuit before the real call.
    ]
    image_path = next((p for p in fixture_candidates if p.exists()), None)
    if image_path is None:
        print("  SKIPPED (no fixture found)")
        return

    # model_config with per-task override so we can assert the value flowed
    # all the way through.
    model_config = {
        "primary": {"provider": "openai", "model": "gpt-5.4-mini"},
        "max_retries": 4,
        "max_response_tokens": 1024,
    }

    # Mock litellm.completion to inspect kwargs and return a fake response.
    # The response must .choices[0].message.content to short-circuit
    # the rest of describe_image.
    fake_choice = mock.MagicMock()
    fake_choice.message.content = "fake vision output"
    fake_response = mock.MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = None  # bypass token tracking path

    with mock.patch.object(
        provider_module.litellm, "completion", return_value=fake_response
    ) as mock_completion:
        result = provider_module.describe_image(
            image_path=image_path,
            prompt="test prompt",
            model_config=model_config,
        )

    assert result == "fake vision output", result
    assert mock_completion.called, "litellm.completion was never invoked"
    _, kwargs = mock_completion.call_args
    assert "num_retries" in kwargs, f"num_retries missing from kwargs: {kwargs.keys()}"
    assert kwargs["num_retries"] == 4, f"expected 4, got {kwargs['num_retries']}"
    print("  PASSED\n")


def test_describe_image_falls_back_to_defaults_when_task_has_no_override():
    """
    When model_config has no explicit max_retries but carries an injected
    _defaults (as load_config does), describe_image should use the
    defaults value.
    """
    print("=== test_describe_image_falls_back_to_defaults_when_task_has_no_override ===")
    from docingest.models import provider as provider_module

    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "test_chart.pptx"
    if not fixture.exists():
        print("  SKIPPED (no fixture found)")
        return

    # Simulate load_config's injection: task dict carries _defaults subkey.
    model_config = {
        "primary": {"provider": "openai", "model": "gpt-5.4-mini"},
        "_defaults": {"max_retries": 3, "max_response_tokens": 1024},
    }

    fake_choice = mock.MagicMock()
    fake_choice.message.content = "ok"
    fake_response = mock.MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = None

    with mock.patch.object(
        provider_module.litellm, "completion", return_value=fake_response
    ) as mock_completion:
        provider_module.describe_image(
            image_path=fixture,
            prompt="x",
            model_config=model_config,
        )

    _, kwargs = mock_completion.call_args
    assert kwargs.get("num_retries") == 3, kwargs
    print("  PASSED\n")


def test_text_completion_passes_num_retries_to_litellm():
    """
    text_completion() is the other core LLM entry point — must also
    propagate num_retries.
    """
    print("=== test_text_completion_passes_num_retries_to_litellm ===")
    from docingest.models import provider as provider_module

    model_config = {
        "primary": {"provider": "openai", "model": "gpt-5.4-mini"},
        "max_retries": 6,
        "max_response_tokens": 512,
    }

    fake_choice = mock.MagicMock()
    fake_choice.message.content = "hello"
    fake_choice.finish_reason = "stop"
    fake_response = mock.MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage = None

    with mock.patch.object(
        provider_module.litellm, "completion", return_value=fake_response
    ) as mock_completion:
        content, finish = provider_module.text_completion(
            prompt="say hi",
            model_config=model_config,
        )

    assert content == "hello"
    assert finish == "stop"
    _, kwargs = mock_completion.call_args
    assert kwargs.get("num_retries") == 6, kwargs
    print("  PASSED\n")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    # Unit
    test_resolve_max_retries_no_config_uses_hardcoded_fallback()
    test_resolve_max_retries_reads_task_level_override()
    test_resolve_max_retries_falls_through_to_defaults()
    test_resolve_max_retries_zero_is_honoured()
    # YAML integration
    test_default_yaml_injects_max_retries_into_every_task()
    # End-to-end kwarg plumbing
    test_describe_image_passes_num_retries_to_litellm()
    test_describe_image_falls_back_to_defaults_when_task_has_no_override()
    test_text_completion_passes_num_retries_to_litellm()
    print("ALL retry tests PASSED")


if __name__ == "__main__":
    main()
