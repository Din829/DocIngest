"""
Provider classes — dataclass wrappers for LLM credentials + model selection.

Part of DocIngest's public Python API. Callers use these to inject API keys
and model choices into `ingest()` / `inspect()` / `refine()` without touching
environment variables or the YAML config:

    import docingest
    result = docingest.ingest(
        "./docs/",
        output="./kb/",
        vision=docingest.GeminiProvider(api_key="..."),
        audio=docingest.DashScopeProvider(api_key="..."),
    )

Design
------
- Each Provider is a thin dataclass holding (provider, model, api_key).
  Keeping them dumb (no dispatch logic) means new providers are a one-line
  addition and downstream code (models/provider.py) stays the single source
  of truth for actual dispatch.
- `.to_model_config()` returns a dict in the same shape the YAML config
  uses (`{"primary": {"provider": ..., "model": ..., "api_key": ...}}`),
  so facade code can slot them straight into `config_overrides` without
  any special casing.
- The `api_key` field is plaintext — models/provider.py picks it up and
  sets the right env var at call time. Existing env-var / YAML paths
  continue to work untouched; Provider objects are purely additive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Base classes — three role-based Provider families
# ---------------------------------------------------------------------------
# Separate base classes rather than one generic class because the three
# roles (vision / audio / text) map to three distinct config sections
# (models.vision / models.audio_transcription / models.chunking_assist).
# Typing each role lets `ingest(vision=..., audio=...)` reject cross-wiring
# a GeminiProvider into `audio=` at type-check time.

@dataclass
class VisionProvider:
    """Base class for Vision LLM providers (image → text)."""
    provider: str
    model: str
    api_key: str | None = None

    def to_model_config(self) -> dict[str, Any]:
        """
        Return a dict shaped like `models.vision` — slots directly into
        config_overrides["models"]["vision"] in the facade.
        """
        return {
            "primary": _build_entry(self.provider, self.model, self.api_key),
        }


@dataclass
class AudioProvider:
    """Base class for audio transcription providers (ASR)."""
    provider: str
    model: str
    api_key: str | None = None

    def to_model_config(self) -> dict[str, Any]:
        """
        Return a dict shaped like `models.audio_transcription` — slots
        directly into config_overrides["models"]["audio_transcription"].
        """
        return {
            "primary": _build_entry(self.provider, self.model, self.api_key),
        }


@dataclass
class TextProvider:
    """
    Base class for text-completion providers (chunking assist / knowledge
    map summary / refine).

    Routes to `models.<task_name>` — the task defaults to "chunking_assist"
    because that's the config section `refine` reads and it's the most
    commonly overridden text task. Override `task` to target a different
    section (e.g. "contextual_summary").
    """
    provider: str
    model: str
    api_key: str | None = None
    task: str = "chunking_assist"

    def to_model_config(self) -> dict[str, Any]:
        return {
            "primary": _build_entry(self.provider, self.model, self.api_key),
        }


def _build_entry(provider: str, model: str, api_key: str | None) -> dict[str, Any]:
    """Build a single model entry dict, omitting api_key when absent."""
    entry: dict[str, Any] = {"provider": provider, "model": model}
    if api_key:
        entry["api_key"] = api_key
    return entry


# ---------------------------------------------------------------------------
# Concrete Vision providers
# ---------------------------------------------------------------------------
# Defaults mirror config/default.yaml so Provider(api_key=...) without a
# model argument produces the same behaviour as the YAML defaults.

@dataclass
class GeminiProvider(VisionProvider):
    provider: str = "google"
    model: str = "gemini-3-flash-preview"
    api_key: str | None = None


@dataclass
class OpenAIProvider(VisionProvider):
    provider: str = "openai"
    model: str = "gpt-5.4-mini"
    api_key: str | None = None


@dataclass
class AnthropicProvider(VisionProvider):
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-20250514"
    api_key: str | None = None


# ---------------------------------------------------------------------------
# Concrete Audio providers
# ---------------------------------------------------------------------------

@dataclass
class DashScopeProvider(AudioProvider):
    provider: str = "dashscope"
    model: str = "qwen3-asr-flash"
    api_key: str | None = None


@dataclass
class WhisperProvider(AudioProvider):
    """OpenAI Whisper via litellm.transcription."""
    provider: str = "openai"
    model: str = "whisper-1"
    api_key: str | None = None


__all__ = [
    "VisionProvider",
    "AudioProvider",
    "TextProvider",
    "GeminiProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    "DashScopeProvider",
    "WhisperProvider",
]
