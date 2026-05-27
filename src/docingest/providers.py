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


@dataclass
class AzureOpenAIProvider(VisionProvider):
    """
    Azure OpenAI Vision provider.

    Azure routes calls through deployments, not raw model names — the `model`
    field here holds the deployment name (what you see under "Deployments" in
    the Azure portal). The underlying GPT-4o / GPT-4o-mini / o-series model
    is whatever that deployment was provisioned with.

    Three Azure-specific fields beyond the base provider are mandatory:
      * api_base:     https://<resource>.openai.azure.com/ (no trailing path)
      * api_version:  e.g. "2024-08-01-preview"; consult Azure docs
      * api_key:      Azure resource key (or set AZURE_API_KEY env)

    Wire format passed to litellm follows litellm's Azure provider contract
    (see https://docs.litellm.ai/docs/providers/azure):
      model="azure/<deployment>", api_base=..., api_version=..., api_key=...
    """
    provider: str = "azure"
    model: str = ""                  # Azure deployment name (NOT a model id)
    api_key: str | None = None
    api_base: str | None = None      # https://<resource>.openai.azure.com/
    api_version: str | None = None   # e.g. "2024-08-01-preview"

    def to_model_config(self) -> dict[str, Any]:
        """
        Build the `models.vision` shape with Azure-specific fields preserved.

        Downstream `_set_api_key` reads `api_base` / `api_version` from the
        primary dict and writes them to AZURE_API_BASE / AZURE_API_VERSION
        env vars; `_resolve_model_name` reads `provider == "azure"` and
        produces the `azure/<deployment>` litellm model string.
        """
        entry = _build_entry(self.provider, self.model, self.api_key)
        if self.api_base:
            entry["api_base"] = self.api_base
        if self.api_version:
            entry["api_version"] = self.api_version
        return {"primary": entry}


@dataclass
class BedrockProvider(VisionProvider):
    """
    AWS Bedrock Vision provider.

    Bedrock routes through AWS's regional endpoints. `model` is Bedrock's
    canonical model id (e.g. ``anthropic.claude-3-sonnet-20240229-v1:0``
    or ``us.anthropic.claude-sonnet-4-20250514-v1:0`` for cross-region
    inference profiles). The ``bedrock/`` prefix is added downstream by
    ``_resolve_model_name``; do NOT include it here.

    Authentication is flexible — pick ONE path:

      * Static credentials: pass ``aws_access_key_id`` + ``aws_secret_access_key``
        (+ ``aws_region_name``). Optional ``aws_session_token`` for STS.
      * Named profile: pass ``aws_profile_name`` (resolved from ~/.aws/config).
      * Bearer token: pass ``api_key`` (mapped to AWS_BEARER_TOKEN_BEDROCK env).
      * IAM role inheritance: pass none of the above — boto3 picks up the
        container/EC2 instance role automatically.

    Wire format follows litellm's Bedrock contract
    (see https://docs.litellm.ai/docs/providers/bedrock):
      model="bedrock/<model_id>", aws_access_key_id=..., aws_region_name=..., ...

    All AWS_* env vars litellm understands are honoured by the underlying
    boto3 client when set externally — this Provider class only writes
    fields that were explicitly passed.
    """
    provider: str = "bedrock"
    model: str = ""                          # e.g. "anthropic.claude-3-sonnet-20240229-v1:0"
    api_key: str | None = None               # bearer token (AWS_BEARER_TOKEN_BEDROCK)
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region_name: str | None = None       # e.g. "us-east-1"
    aws_session_token: str | None = None     # STS temporary credentials
    aws_profile_name: str | None = None      # named profile from ~/.aws/config

    def to_model_config(self) -> dict[str, Any]:
        """
        Build the `models.vision` shape with Bedrock-specific fields preserved.

        Only fields that the caller actually set are emitted — this lets
        a caller relying on container IAM role / external env vars pass
        just ``BedrockProvider(model="...")`` without polluting env with
        empty strings downstream.
        """
        entry = _build_entry(self.provider, self.model, self.api_key)
        for field_name in (
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_region_name",
            "aws_session_token",
            "aws_profile_name",
        ):
            value = getattr(self, field_name)
            if value:
                entry[field_name] = value
        return {"primary": entry}


@dataclass
class VertexAIProvider(VisionProvider):
    """
    Google Vertex AI Vision provider.

    Vertex routes through Google Cloud regional endpoints. `model` is the
    Vertex model id (e.g. ``gemini-2.5-pro``, ``gemini-1.5-flash``). The
    ``vertex_ai/`` prefix is added downstream by ``_resolve_model_name``;
    do NOT include it here.

    Two fields are MANDATORY (per litellm's Vertex contract):
      * vertex_project:  GCP project id, e.g. "my-gcp-project"
      * vertex_location: region, e.g. "us-central1"

    Authentication:
      * vertex_credentials (optional): service-account JSON FILE PATH or
        a raw JSON STRING. When omitted, litellm falls through to the
        standard ``GOOGLE_APPLICATION_CREDENTIALS`` env var, or to gcloud
        ADC (workload identity / metadata server in GCP).
      * ``api_key`` is NOT used by Vertex — leave it None. Setting it has
        no effect (kept on the dataclass for VisionProvider base compat).

    Wire format follows litellm's Vertex contract
    (see https://docs.litellm.ai/docs/providers/vertex):
      model="vertex_ai/<model_id>", vertex_project=..., vertex_location=...,
      vertex_credentials=...
    """
    provider: str = "vertex_ai"
    model: str = ""                          # e.g. "gemini-2.5-pro"
    api_key: str | None = None               # unused by Vertex (kept for base compat)
    vertex_project: str | None = None
    vertex_location: str | None = None
    vertex_credentials: str | None = None    # service account JSON file path OR JSON string

    def to_model_config(self) -> dict[str, Any]:
        """
        Build the `models.vision` shape with Vertex-specific fields preserved.

        Empty optional fields are omitted (same rationale as BedrockProvider).
        """
        entry = _build_entry(self.provider, self.model, self.api_key)
        for field_name in ("vertex_project", "vertex_location", "vertex_credentials"):
            value = getattr(self, field_name)
            if value:
                entry[field_name] = value
        return {"primary": entry}


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
