"""
AI Model Provider — multi-provider abstraction with fallback.

Wraps litellm to provide a unified interface for Vision and text completions.
Supports primary + fallback model chains configured via YAML.

Design:
  - Each AI task (vision, chunking_assist, contextual_summary) has its own
    model config with primary + optional fallback.
  - Primary model fails → automatically retry with fallback model.
  - API keys read from environment variables (never stored in config files).
"""

from __future__ import annotations

import os
import base64
from pathlib import Path
from typing import Any, Sequence

import litellm

from .token_tracker import token_tracker


# Suppress litellm's verbose logging by default
litellm.suppress_debug_info = True


# Hard fallback used only when neither model_config nor an explicit caller
# argument specifies max_response_tokens. Every real caller should have
# models.defaults.max_response_tokens populated from config/default.yaml;
# this constant guards against misconfigured tests / callers only. Kept at
# the provider ceiling (Gemini 3 Flash / 3.1 Pro = 65536) so a misconfigured
# deployment still gets full headroom — output tokens cost the same whether
# the cap is 32K or 64K (you only pay for tokens actually emitted).
_HARD_FALLBACK_MAX_TOKENS = 65536

# Hard fallback for network-level retry count when config is missing.
# litellm itself defaults to 0 (no retries), which is too brittle for
# production — a single rate-limit blip or TCP reset drops a page of
# Vision output. Using 2 matches litellm's DEFAULT_MAX_RETRIES constant,
# i.e. the behaviour you'd get if you invoked litellm directly without
# passing num_retries.
_HARD_FALLBACK_MAX_RETRIES = 2


def resolve_max_tokens(
    model_config: dict[str, Any] | None,
    explicit: int | None = None,
) -> int:
    """
    Resolve the max_response_tokens to use for an LLM call.

    Priority (first non-None wins):
      1. explicit caller argument
      2. model_config["max_response_tokens"]   (per-task override)
      3. model_config["_defaults"]["max_response_tokens"]  (global default
         injected by load_config so every task inherits models.defaults)
      4. _HARD_FALLBACK_MAX_TOKENS              (safety net)

    Keeping this as the single source of truth means future tasks add no new
    hardcoded numbers — just omit max_tokens and they inherit automatically.
    """
    if explicit is not None:
        return int(explicit)
    if model_config:
        if model_config.get("max_response_tokens") is not None:
            return int(model_config["max_response_tokens"])
        defaults = model_config.get("_defaults") or {}
        if defaults.get("max_response_tokens") is not None:
            return int(defaults["max_response_tokens"])
    return _HARD_FALLBACK_MAX_TOKENS


def resolve_max_retries(model_config: dict[str, Any] | None) -> int:
    """
    Resolve the number of network-level retries litellm should attempt.

    Priority (first non-None wins):
      1. model_config["max_retries"]                   (per-task override)
      2. model_config["_defaults"]["max_retries"]      (global default
         injected by load_config so every task inherits models.defaults)
      3. _HARD_FALLBACK_MAX_RETRIES                    (safety net: 2)

    This controls litellm's built-in retry loop for TRANSIENT errors:
    rate limits, 5xx responses, connection resets, timeouts. It is NOT
    related to text_completion's `retry_on_truncation` layer, which sits
    on top and handles finish_reason=="length" at the application level.
    The two retry mechanisms are orthogonal — both can fire for the same
    call (network retry first, then if the server eventually replies but
    the response is truncated, the truncation retry kicks in).

    Mirrors the shape of resolve_max_tokens so both resolver helpers stay
    visually adjacent in the codebase and obvious to readers.
    """
    if model_config:
        if model_config.get("max_retries") is not None:
            return int(model_config["max_retries"])
        defaults = model_config.get("_defaults") or {}
        if defaults.get("max_retries") is not None:
            return int(defaults["max_retries"])
    return _HARD_FALLBACK_MAX_RETRIES


def _extract_finish_reason(response) -> str:
    """Best-effort extraction of finish_reason from a litellm response."""
    try:
        return str(response.choices[0].finish_reason or "")
    except (AttributeError, IndexError):
        return ""


def _record_usage(response, model_name: str) -> None:
    """Extract usage from litellm response and record to tracker."""
    usage = getattr(response, "usage", None)
    if usage is not None:
        token_tracker.record(
            model=model_name,
            prompt=getattr(usage, "prompt_tokens", 0) or 0,
            completion=getattr(usage, "completion_tokens", 0) or 0,
        )


def _resolve_model_name(provider: str, model: str) -> str:
    """
    Convert (provider, model) to litellm's model string format.

    litellm uses prefixed model names for non-OpenAI providers:
      - google: "gemini/gemini-3-flash" (Gemini API direct)
      - anthropic: "anthropic/claude-sonnet-4-20250514"
      - azure: "azure/<deployment_name>" — `model` is an Azure deployment
        name, not a raw model id (see AzureOpenAIProvider docstring)
      - bedrock: "bedrock/<aws_model_id>" — e.g. anthropic.claude-...-v1:0
      - vertex_ai: "vertex_ai/<model_id>" — distinct from "google" which
        uses the public Gemini API (Vertex is GCP-hosted, IAM-controlled)
      - openai: "gpt-5.4-mini" (no prefix needed)

    The "vertex" alias previously routed to "gemini/" — that was incorrect
    semantically (Vertex != public Gemini API) but harmless because no
    Vertex provider class existed. Now "vertex_ai" is the canonical alias.
    Old configs using `provider: "vertex"` still route to "gemini/" for
    backwards compatibility (they couldn't have been calling Vertex anyway).
    """
    provider_lower = provider.lower()
    if provider_lower in ("google", "gemini", "vertex"):
        return f"gemini/{model}" if not model.startswith("gemini/") else model
    if provider_lower in ("anthropic", "claude"):
        return f"anthropic/{model}" if not model.startswith("anthropic/") else model
    if provider_lower == "azure":
        return f"azure/{model}" if not model.startswith("azure/") else model
    if provider_lower == "bedrock":
        return f"bedrock/{model}" if not model.startswith("bedrock/") else model
    if provider_lower == "vertex_ai":
        return f"vertex_ai/{model}" if not model.startswith("vertex_ai/") else model
    # OpenAI and others: use model name directly
    return model


# Provider → canonical env var name for the primary `api_key` field.
# Used when a caller passes a plaintext api_key without also specifying
# api_key_env (the common case when a downstream library injects credentials
# via the Provider class instead of editing .env). Kept in sync with the
# providers DocIngest's config supports; unknown providers fall through and
# require an explicit api_key_env.
#
# Bedrock's "api_key" is a bearer token (litellm reads it from
# AWS_BEARER_TOKEN_BEDROCK). Vertex AI has no api_key concept — auth is via
# service-account JSON, handled below in _PROVIDER_EXTRA_ENV_MAP.
_PROVIDER_TO_ENV_KEY = {
    "google": "GEMINI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "vertex": "GEMINI_API_KEY",        # legacy alias; vertex_ai (below) is the new one
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "azure": "AZURE_API_KEY",          # litellm's standard, NOT AZURE_OPENAI_API_KEY
    "bedrock": "AWS_BEARER_TOKEN_BEDROCK",  # only used if caller picks bearer-token auth
}

# Cloud-provider extra fields → env vars they need to land in for litellm
# to find them. Data-driven so adding a new cloud is one entry, not a code
# branch in _set_api_key. Field names are the keys downstream Provider
# classes put into model_config (see to_model_config in providers.py);
# env var names follow litellm's own contract (verified against
# https://docs.litellm.ai/docs/providers/<provider>).
_PROVIDER_EXTRA_ENV_MAP: dict[str, dict[str, str]] = {
    "azure": {
        "api_base": "AZURE_API_BASE",
        "api_version": "AZURE_API_VERSION",
    },
    "bedrock": {
        "aws_access_key_id": "AWS_ACCESS_KEY_ID",
        "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
        "aws_region_name": "AWS_REGION_NAME",
        "aws_session_token": "AWS_SESSION_TOKEN",
        "aws_profile_name": "AWS_PROFILE",
    },
    "vertex_ai": {
        "vertex_project": "VERTEXAI_PROJECT",
        "vertex_location": "VERTEXAI_LOCATION",
        # vertex_credentials accepts a filepath OR a JSON string; litellm
        # reads GOOGLE_APPLICATION_CREDENTIALS for the filepath form, which
        # is what most callers want. A JSON-string credential bypasses this
        # path and must be passed via litellm function args — out of scope
        # for the env-mirror approach used here.
        "vertex_credentials": "GOOGLE_APPLICATION_CREDENTIALS",
    },
}


def _set_api_key(model_config: dict[str, Any]) -> None:
    """
    Mirror the model entry's credential fields to the env vars litellm reads.

    Two parallel paths:

    1. **Primary `api_key`** (first match wins):
       - Plaintext `api_key` in model_config → written to the env var
         resolved from ``api_key_env`` OR ``_PROVIDER_TO_ENV_KEY[provider]``.
       - ``api_key_env`` already populated → no-op (classic path).

    2. **Cloud-provider extras** (e.g. Azure endpoint, AWS region, Vertex
       project): every field in ``_PROVIDER_EXTRA_ENV_MAP[provider]`` that
       the caller actually set is mirrored to the matching env var. Empty/
       missing fields are skipped, so a caller relying on ambient AWS / GCP
       credentials (container IAM role, gcloud ADC, ~/.aws/config) is not
       overridden.

    litellm reads standard env vars (GEMINI_API_KEY, OPENAI_API_KEY,
    AZURE_API_KEY, AWS_ACCESS_KEY_ID, VERTEXAI_PROJECT, ...) so we only
    need to ensure the right env vars hold the right values before
    litellm.completion() runs.
    """
    explicit = model_config.get("api_key")
    env_key = model_config.get("api_key_env")
    provider = str(model_config.get("provider", "")).lower()

    # Mirror provider-specific extra fields. Done unconditionally because
    # callers using ambient cloud credentials still benefit from having
    # region / project / location set explicitly.
    for field_name, env_var in _PROVIDER_EXTRA_ENV_MAP.get(provider, {}).items():
        value = model_config.get(field_name)
        if value:
            os.environ[env_var] = value

    if explicit:
        # Target env var: prefer api_key_env when present, otherwise infer
        # from the provider name. If neither resolves, we silently skip —
        # the subsequent litellm call will surface a clear auth error.
        target = env_key
        if not target:
            target = _PROVIDER_TO_ENV_KEY.get(provider)
        if target:
            os.environ[target] = explicit
        return

    if env_key and os.environ.get(env_key):
        # env var already populated — litellm will pick it up automatically.
        pass


# ---------------------------------------------------------------------------
# Vision completion (image → text description)
# ---------------------------------------------------------------------------

def describe_image(
    image_path: Path | str,
    prompt: str = "Describe this image in detail. If it's a chart or graph, extract all data points and trends.",
    model_config: dict[str, Any] | None = None,
    max_tokens: int | None = None,
) -> str:
    """
    Send an image to a Vision model and get a text description.

    Args:
        image_path: Path to the image file.
        prompt: Instruction for the Vision model.
        model_config: Model config dict with 'primary' and optional 'fallback'.
            Example: {"primary": {"provider": "google", "model": "gemini-3-flash", ...},
                      "fallback": {"provider": "openai", "model": "gpt-5.4-mini", ...}}

    Returns:
        Text description of the image.

    Raises:
        RuntimeError: If both primary and fallback fail.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    # Build message — one text part + one image part.
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            _encode_image_for_litellm(image_path),
        ],
    }]

    # Try primary, then fallback
    effective_max_tokens = resolve_max_tokens(model_config, max_tokens)
    # Network-level retry (rate limits, 5xx, connection resets). Per-task
    # override under the same model_config lets Vision / ASR / text
    # completion each tune their own retry budget independently.
    effective_num_retries = resolve_max_retries(model_config)
    models_to_try = _build_model_chain(model_config)
    last_error = None

    for model_entry in models_to_try:
        _set_api_key(model_entry)
        model_name = _resolve_model_name(
            model_entry.get("provider", "openai"),
            model_entry.get("model", "gpt-5.4-mini"),
        )
        try:
            response = litellm.completion(
                model=model_name,
                messages=messages,
                max_tokens=effective_max_tokens,
                num_retries=effective_num_retries,
            )
            _record_usage(response, model_name)
            content = response.choices[0].message.content
            return content.strip() if content else ""
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(
        f"Vision description failed for {image_path.name}. "
        f"Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}


def _encode_image_for_litellm(image_path: Path) -> dict[str, Any]:
    """
    Encode an image file as a litellm-compatible content part.

    Reused by single-image describe_image and multi-image
    describe_images_batched so the base64 + mime detection logic lives in
    one place.
    """
    image_bytes = image_path.read_bytes()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    mime_type = _MIME_BY_EXT.get(image_path.suffix.lower(), "image/png")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"},
    }


def describe_images_batched(
    image_paths: Sequence[Path | str],
    prompt: str,
    model_config: dict[str, Any] | None = None,
    max_tokens: int | None = None,
) -> str:
    """
    Send MULTIPLE images to a Vision model in a single API call.

    Used for xlsx whose single sheet renders to multiple PDF pages: sending
    all pages of one sheet together lets Vision stitch cross-page table
    continuations (per fair-comparison experiment: 90% vs 18% substring
    coverage and 10/10 vs 1/10 cross-page continuity score against
    per-page calls on USDM-style sheets, see pipeline._enrich_with_vision).

    Args:
        image_paths: Ordered list of page image files. Order = the order
            Vision sees them. Empty list returns "".
        prompt: Single instruction prompt covering all images. Caller is
            responsible for any per-image markers inside the prompt text
            (e.g. "[Page 8 below:]") if it wants to disambiguate.
        model_config: Same shape as describe_image's model_config.
        max_tokens: Caller-provided output cap. None falls through to
            model_config / models.defaults.

    Returns:
        Vision response text. Same shape as describe_image — caller
        receives one merged response covering all input pages.

    Raises:
        FileNotFoundError: when any image path doesn't exist (fail loud
            so the caller's fallback path can fire cleanly).
        RuntimeError: when primary AND fallback providers both fail. Same
            contract as describe_image.
    """
    if not image_paths:
        return ""

    paths = [Path(p) for p in image_paths]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Page images not found: {missing}")

    # Build a single message containing text + N images, in caller order.
    # litellm passes this through to Gemini / OpenAI / Anthropic which all
    # accept multi-image content arrays.
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for p in paths:
        content.append(_encode_image_for_litellm(p))
    messages = [{"role": "user", "content": content}]

    effective_max_tokens = resolve_max_tokens(model_config, max_tokens)
    effective_num_retries = resolve_max_retries(model_config)
    models_to_try = _build_model_chain(model_config)
    last_error = None

    for model_entry in models_to_try:
        _set_api_key(model_entry)
        model_name = _resolve_model_name(
            model_entry.get("provider", "openai"),
            model_entry.get("model", "gpt-5.4-mini"),
        )
        try:
            response = litellm.completion(
                model=model_name,
                messages=messages,
                max_tokens=effective_max_tokens,
                num_retries=effective_num_retries,
            )
            _record_usage(response, model_name)
            content_out = response.choices[0].message.content
            return content_out.strip() if content_out else ""
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(
        f"Batched Vision description failed for {len(paths)} image(s). "
        f"Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Text completion (for chunking assist, contextual summary, etc.)
# ---------------------------------------------------------------------------

def text_completion(
    prompt: str,
    system_prompt: str = "",
    model_config: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    retry_on_truncation: bool | None = None,
) -> tuple[str, str]:
    """
    Send a text prompt to an LLM and get a response.

    Used for: AI-assisted chunking, knowledge_map summary/guide, refine, etc.

    Args:
        prompt: The user prompt.
        system_prompt: Optional system instruction.
        model_config: Model config dict with 'primary' and optional 'fallback'.
            May also carry 'max_response_tokens' (per-task override) and a
            '_defaults' subdict for inherited global defaults.
        max_tokens: Explicit cap; when None (default), resolve_max_tokens()
            derives from model_config or global defaults.
        retry_on_truncation: When the first call stops with finish_reason="length"
            (response cut off by token budget), retry once with a larger budget
            (models.defaults.retry_max_tokens, falling back to 2 × the original
            cap if unset).
              True  → always retry on truncation.
              False → never retry; return the truncated response as-is.
              None (default) → read models.defaults.retry_on_truncation injected
                               into model_config["_defaults"] by load_config.
                               Preserves pre-existing per-task opt-in behaviour.

    Returns:
        (content, finish_reason) — finish_reason is the litellm-reported
        stop reason ("stop" | "length" | "content_filter" | ""). Callers
        MUST check for "length" before trusting the content: an LLM that
        stops on "length" has been cut off and may have emitted a syntactically
        incomplete response (e.g. unterminated YAML list). Even with retry
        enabled, finish_reason can still be "length" when the retry budget
        was also exhausted.

    Raises:
        RuntimeError: If both primary and fallback fail.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    effective_max_tokens = resolve_max_tokens(model_config, max_tokens)
    # Network-level retry; independent of the truncation retry below. When
    # text_completion recurses (for a truncation retry), the recursion passes
    # the same model_config, so num_retries is naturally honoured in both
    # the initial and retry calls.
    effective_num_retries = resolve_max_retries(model_config)
    models_to_try = _build_model_chain(model_config)
    last_error = None

    for model_entry in models_to_try:
        _set_api_key(model_entry)
        model_name = _resolve_model_name(
            model_entry.get("provider", "openai"),
            model_entry.get("model", "gpt-5.4-mini"),
        )
        try:
            response = litellm.completion(
                model=model_name,
                messages=messages,
                max_tokens=effective_max_tokens,
                num_retries=effective_num_retries,
            )
            _record_usage(response, model_name)
            content = response.choices[0].message.content
            finish_reason = _extract_finish_reason(response)
            result_text = content.strip() if content else ""

            # Truncation retry layer — centralised so every text-completion
            # consumer (knowledge_map, refine, future callers) inherits the
            # same behaviour. Only kicks in when the provider call itself
            # succeeded but the LLM stopped on the length boundary.
            if finish_reason == "length":
                should_retry = retry_on_truncation
                if should_retry is None:
                    defaults = (model_config or {}).get("_defaults") or {}
                    should_retry = bool(defaults.get("retry_on_truncation", False))

                if should_retry:
                    defaults = (model_config or {}).get("_defaults") or {}
                    retry_budget = defaults.get("retry_max_tokens")
                    if not retry_budget:
                        # Safety fallback when retry_max_tokens is missing:
                        # double the effective budget. Keeps retries bounded
                        # but avoids giving up at the same limit that just failed.
                        retry_budget = effective_max_tokens * 2
                    import logging
                    logging.getLogger(__name__).warning(
                        f"text_completion truncated (finish_reason=length); "
                        f"retrying once with max_tokens={retry_budget}."
                    )
                    # Recurse with retry disabled to guarantee at most one retry.
                    return text_completion(
                        prompt=prompt,
                        system_prompt=system_prompt,
                        model_config=model_config,
                        max_tokens=retry_budget,
                        retry_on_truncation=False,
                    )

            return result_text, finish_reason
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"Text completion failed. Last error: {last_error}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_model_chain(model_config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """
    Build ordered list of models to try (primary → fallback).

    Args:
        model_config: Config dict with 'primary' and optional 'fallback' keys.

    Returns:
        List of model entry dicts to try in order.
    """
    if not model_config:
        # No config → use default
        return [{"provider": "openai", "model": "gpt-5.4-mini"}]

    chain = []
    if "primary" in model_config:
        chain.append(model_config["primary"])
    if "fallback" in model_config:
        chain.append(model_config["fallback"])

    # If config has provider/model directly (flat format), use it
    if not chain and "provider" in model_config:
        chain.append(model_config)

    return chain if chain else [{"provider": "openai", "model": "gpt-5.4-mini"}]
