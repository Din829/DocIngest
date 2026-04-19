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
from typing import Any

import litellm

from .token_tracker import token_tracker


# Suppress litellm's verbose logging by default
litellm.suppress_debug_info = True


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
      - google: "gemini/gemini-3-flash"
      - anthropic: "anthropic/claude-sonnet-4-20250514"
      - openai: "gpt-5.4-mini" (no prefix needed)
    """
    provider_lower = provider.lower()
    if provider_lower in ("google", "gemini", "vertex"):
        return f"gemini/{model}" if not model.startswith("gemini/") else model
    if provider_lower in ("anthropic", "claude"):
        return f"anthropic/{model}" if not model.startswith("anthropic/") else model
    # OpenAI and others: use model name directly
    return model


def _set_api_key(model_config: dict[str, Any]) -> None:
    """Set the API key from environment variable if specified in config."""
    env_key = model_config.get("api_key_env")
    if env_key and os.environ.get(env_key):
        # litellm reads standard env vars (GEMINI_API_KEY, OPENAI_API_KEY, etc.)
        # Just verify it's set — litellm will pick it up automatically.
        pass


# ---------------------------------------------------------------------------
# Vision completion (image → text description)
# ---------------------------------------------------------------------------

def describe_image(
    image_path: Path | str,
    prompt: str = "Describe this image in detail. If it's a chart or graph, extract all data points and trends.",
    model_config: dict[str, Any] | None = None,
    max_tokens: int = 32768,
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

    # Read and encode image
    image_bytes = image_path.read_bytes()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    # Detect mime type from extension
    ext = image_path.suffix.lower()
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".tiff": "image/tiff",
        ".bmp": "image/bmp",
    }
    mime_type = mime_map.get(ext, "image/png")

    # Build message
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64_image}",
                },
            },
        ],
    }]

    # Try primary, then fallback
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
                max_tokens=max_tokens,
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
# Text completion (for chunking assist, contextual summary, etc.)
# ---------------------------------------------------------------------------

def text_completion(
    prompt: str,
    system_prompt: str = "",
    model_config: dict[str, Any] | None = None,
    max_tokens: int = 500,
) -> str:
    """
    Send a text prompt to an LLM and get a response.

    Used for: AI-assisted chunking, contextual summary, etc.

    Args:
        prompt: The user prompt.
        system_prompt: Optional system instruction.
        model_config: Model config dict with 'primary' and optional 'fallback'.
        max_tokens: Maximum response length.

    Returns:
        Model response text.

    Raises:
        RuntimeError: If both primary and fallback fail.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

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
                max_tokens=max_tokens,
            )
            _record_usage(response, model_name)
            content = response.choices[0].message.content
            return content.strip() if content else ""
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
