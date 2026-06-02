"""
Model unification — one model to rule them all (except ASR / embedding),
while staying fully overridable per task and via env. Guards against the
"env/config flexible but a model name hard-coded in code" split.

No LLM calls — pure config resolution + the fail-loud chain builder. Run:
  python tests/unit/test_model_unification.py
"""

from __future__ import annotations

import os

from docingest.config import load_config
from docingest.models.provider import _build_model_chain


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _model(task_cfg) -> str:
    return ((task_cfg or {}).get("primary") or {}).get("model", "")


# Tasks that MUST inherit the unified default model.
_UNIFIED_TASKS = ["vision", "chunking_assist", "contextual_summary"]


def test_one_model_everywhere():
    """Out of the box, every text/vision task resolves to the SAME model
    (the one defined once in models.defaults.primary)."""
    print("test_one_model_everywhere")
    cfg = load_config()
    m = cfg["models"]
    default_model = _model(m["defaults"])
    _check(bool(default_model), f"models.defaults.primary.model is set ({default_model})")

    for t in _UNIFIED_TASKS:
        _check(_model(m[t]) == default_model, f"{t} inherits default model ({default_model})")

    # graph.llm too (lives outside `models`) — the "graph configured backwards"
    # inconsistency must be gone.
    _check(_model(cfg["graph"]["llm"]) == default_model,
           f"graph.llm inherits default model (no longer inverse)")


def test_change_one_place_changes_everywhere():
    """The whole point: editing models.defaults.primary (here via an env
    override, the most aggressive path) re-points EVERY inheriting task — no
    code, no per-task edits. Proves there is no hard-coded model masking it."""
    print("test_change_one_place_changes_everywhere")
    env_key = "DOCINGEST__models__defaults__primary__model"
    prev = os.environ.get(env_key)
    os.environ[env_key] = "gemini-3-pro-preview"  # pretend we switched models
    try:
        cfg = load_config()
        m = cfg["models"]
        _check(_model(m["defaults"]) == "gemini-3-pro-preview", "env changed defaults.primary")
        for t in _UNIFIED_TASKS:
            _check(_model(m[t]) == "gemini-3-pro-preview",
                   f"{t} followed the single-source change (no hard-coded model)")
        _check(_model(cfg["graph"]["llm"]) == "gemini-3-pro-preview",
               "graph.llm followed too")
    finally:
        if prev is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = prev


def test_per_task_override_still_wins():
    """Flexibility preserved: a task that declares its own primary keeps it,
    NOT the unified default. (env-set a per-task primary and confirm only that
    task diverges.)"""
    print("test_per_task_override_still_wins")
    env_key = "DOCINGEST__models__vision__primary__model"
    prev = os.environ.get(env_key)
    os.environ[env_key] = "gpt-4o"
    try:
        cfg = load_config()
        m = cfg["models"]
        _check(_model(m["vision"]) == "gpt-4o", "vision uses its own override")
        # other tasks unaffected → still the default
        default_model = _model(m["defaults"])
        _check(_model(m["chunking_assist"]) == default_model,
               "chunking_assist still on the default (override is task-local)")
    finally:
        if prev is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = prev


def test_asr_and_embedding_untouched():
    """ASR and embedding are role-specific models — unification must NOT pull
    them onto the text/vision default."""
    print("test_asr_and_embedding_untouched")
    cfg = load_config()
    m = cfg["models"]
    _check(_model(m["audio_transcription"]) == "qwen3-asr-flash", "ASR stays qwen3-asr-flash")
    _check((m["audio_transcription"].get("fallback") or {}).get("model") == "whisper-1",
           "ASR fallback stays whisper-1")
    _check(cfg["graph"]["embedding"]["model"] == "text-embedding-3-small",
           "embedding stays text-embedding-3-small")


def test_per_task_token_override_preserved():
    """Inheriting the model must NOT wipe a task's own non-model overrides
    (e.g. vision.max_response_tokens, graph.llm.max_response_tokens)."""
    print("test_per_task_token_override_preserved")
    cfg = load_config()
    _check(cfg["models"]["vision"].get("max_response_tokens") == 65536,
           "vision keeps its own max_response_tokens")
    _check(cfg["graph"]["llm"].get("max_response_tokens") == 65536,
           "graph.llm keeps its own max_response_tokens")


def test_fail_loud_on_missing_model():
    """No hard-coded model name to fall back on: an empty / model-less config
    raises, instead of silently substituting some model (which would defeat a
    unified-model setup and mask a real config error)."""
    print("test_fail_loud_on_missing_model")
    for bad in [None, {}, {"primary": {"provider": "openai"}}, {"primary": {"model": "x"}}]:
        try:
            _build_model_chain(bad)
            raise AssertionError(f"_build_model_chain({bad!r}) should have raised")
        except ValueError:
            print(f"  ok: _build_model_chain({bad!r}) fails loud")

    # A valid chain still builds and is guaranteed provider+model.
    chain = _build_model_chain({
        "primary": {"provider": "google", "model": "gemini-3-flash-preview"},
        "fallback": {"provider": "openai", "model": "gpt-5.4-mini"},
    })
    _check(len(chain) == 2 and all(e.get("provider") and e.get("model") for e in chain),
           "valid config → 2-entry chain, all provider+model present")


if __name__ == "__main__":
    test_one_model_everywhere()
    test_change_one_place_changes_everywhere()
    test_per_task_override_still_wins()
    test_asr_and_embedding_untouched()
    test_per_task_token_override_preserved()
    test_fail_loud_on_missing_model()
    print("\n=== ALL MODEL-UNIFICATION TESTS PASSED ===")
