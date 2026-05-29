"""
Regression guard for models.provider credential handling.

Background: credentials used to be mirrored into the global ``os.environ`` by
``_set_api_key`` right before each ``litellm.completion`` call. Under
concurrency (a long-running host running several ``ingest()`` calls with
DIFFERENT keys) that races — one thread's key clobbers another's between the
write and litellm's read. The fix routes credentials through
``_resolve_call_credentials`` as PER-CALL kwargs that never touch global state.

This test locks in three properties (no mocking of the unit under test — only
the network egress ``litellm.completion`` is replaced to observe what kwargs it
receives):

  A) Concurrent calls never cross-talk: 60 concurrent calls, each with its own
     key, and litellm always receives the caller's own key — even when the
     credential-resolution→litellm gap is artificially widened (the exact gap
     that produced 59/60 cross-talk before the fix).
  B) Backward compat: the env/.env path (no plaintext api_key in model_config)
     produces ZERO call kwargs, so litellm reads from the environment exactly
     as it always did.
  C) Cloud-provider matrix (azure / bedrock / vertex_ai): credentials map to the
     correct litellm kwarg names, Vertex never forwards api_key, and resolution
     mutates os.environ NOT AT ALL.

Run:
    python tests/unit/test_provider_concurrency.py
"""

from __future__ import annotations

import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import litellm
from docingest.models import provider as P


class _FakeResponse:
    """Minimal litellm-shaped response so text_completion returns cleanly."""
    class _Choice:
        class _Msg:
            content = "ok"
        message = _Msg()
        finish_reason = "stop"
    class _Usage:
        prompt_tokens = 1
        completion_tokens = 1
    choices = [_Choice()]
    usage = _Usage()


def test_concurrent_no_credential_crosstalk():
    """60 concurrent calls with distinct keys → litellm always sees the right key."""
    print("=== test_concurrent_no_credential_crosstalk ===")
    observations: list[tuple[str, str | None, str]] = []
    obs_lock = threading.Lock()

    real_resolve = P._resolve_call_credentials

    def widened_resolve(mc):
        # Real resolution, then widen the resolve→litellm-read gap. Pre-fix this
        # is exactly where a concurrent os.environ write clobbered the key.
        # Post-fix the credential lives in the returned local dict, immune to it.
        creds = real_resolve(mc)
        time.sleep(0.01)
        return creds

    def fake_completion(model, messages, max_tokens=None, num_retries=None,
                        api_key=None, **kwargs):
        expected = messages[-1]["content"]
        with obs_lock:
            observations.append((expected, api_key, threading.current_thread().name))
        return _FakeResponse()

    P._resolve_call_credentials = widened_resolve
    litellm.completion = fake_completion
    try:
        def run_one(idx: int):
            my_key = f"sk-KEY-{idx:04d}"
            cfg = {"primary": {"provider": "openai", "model": "gpt-5.4-mini",
                               "api_key": my_key}}
            P.text_completion(prompt=my_key, model_config=cfg)

        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(run_one, i) for i in range(60)]
            for f in as_completed(futs):
                f.result()
    finally:
        P._resolve_call_credentials = real_resolve

    mism = [o for o in observations if o[0] != o[1]]
    assert len(observations) == 60, f"expected 60 calls, got {len(observations)}"
    assert not mism, (
        f"credential cross-talk: {len(mism)}/{len(observations)} calls saw the "
        f"wrong key, e.g. {mism[:3]}"
    )
    print(f"  {len(observations)} concurrent calls, 0 cross-talk  PASSED\n")


def test_env_path_emits_no_call_kwargs():
    """No plaintext api_key → resolve returns {} and litellm gets api_key=None."""
    print("=== test_env_path_emits_no_call_kwargs ===")
    captured: dict = {}

    def fake_completion(model, messages, max_tokens=None, num_retries=None,
                        api_key=None, **kwargs):
        captured["api_key_kwarg"] = api_key
        return _FakeResponse()

    litellm.completion = fake_completion

    cfg = {"primary": {"provider": "openai", "model": "gpt-5.4-mini",
                       "api_key_env": "OPENAI_API_KEY"}}
    creds = P._resolve_call_credentials(cfg["primary"])
    assert creds == {}, f"env path must emit no call kwargs, got {creds}"

    before = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "sk-from-env"
    try:
        P.text_completion(prompt="hi", model_config=cfg)
    finally:
        if before is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = before

    assert captured.get("api_key_kwarg") is None, (
        "env path must NOT pass api_key as a kwarg (litellm reads it from env)"
    )
    print("  env/.env path unchanged (litellm reads env itself)  PASSED\n")


def test_cloud_provider_matrix_and_no_env_mutation():
    """azure / bedrock / vertex_ai map to correct kwargs; os.environ untouched."""
    print("=== test_cloud_provider_matrix_and_no_env_mutation ===")
    env_snapshot = dict(os.environ)

    cases = [
        ("azure",
         {"provider": "azure", "model": "my-deploy", "api_key": "az-key",
          "api_base": "https://r.openai.azure.com/",
          "api_version": "2024-08-01-preview"},
         {"api_key": "az-key", "api_base": "https://r.openai.azure.com/",
          "api_version": "2024-08-01-preview"}),
        ("bedrock",
         {"provider": "bedrock", "model": "anthropic.claude-3:0",
          "aws_access_key_id": "AKIA", "aws_secret_access_key": "sec",
          "aws_region_name": "us-east-1", "aws_profile_name": "prof"},
         {"aws_access_key_id": "AKIA", "aws_secret_access_key": "sec",
          "aws_region_name": "us-east-1", "aws_profile": "prof"}),
        ("vertex_ai",
         {"provider": "vertex_ai", "model": "gemini-2.5-pro", "api_key": "ignored",
          "vertex_project": "proj", "vertex_location": "us-central1",
          "vertex_credentials": '{"type":"service_account"}'},
         {"vertex_project": "proj", "vertex_location": "us-central1",
          "vertex_credentials": '{"type":"service_account"}'}),  # vertex: no api_key
    ]
    for name, mc, expected in cases:
        got = P._resolve_call_credentials(mc)
        assert got == expected, f"{name}: expected {expected}, got {got}"

    assert dict(os.environ) == env_snapshot, (
        "_resolve_call_credentials must not mutate os.environ"
    )
    print("  azure/bedrock/vertex kwargs correct, os.environ untouched  PASSED\n")


def main():
    test_concurrent_no_credential_crosstalk()
    test_env_path_emits_no_call_kwargs()
    test_cloud_provider_matrix_and_no_env_mutation()
    print("ALL provider concurrency TESTS PASSED")


if __name__ == "__main__":
    main()
