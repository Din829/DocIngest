"""
Audio transcription provider — multi-engine abstraction.

Same architecture as provider.py (describe_image / text_completion):
primary + fallback chain, each entry specifies provider + model + api_key.

Supported providers
~~~~~~~~~~~~~~~~~~~
  * **dashscope** — Qwen3-ASR-Flash via DashScope SDK. Accepts local files
    (auto-uploaded) or public URLs. Returns full transcript + optional
    timestamps. Default.
  * **openai** — Whisper via litellm.transcription(). Accepts local files.
    Broad language support.
  * Any other litellm-compatible provider — falls through to
    litellm.transcription() which may or may not work.

Config (mirrors models.vision structure):
  models:
    audio_transcription:
      primary:
        provider: "dashscope"
        model: "qwen3-asr-flash-filetrans"
        api_key_env: "DASHSCOPE_API_KEY"
      fallback:
        provider: "openai"
        model: "whisper-1"
        api_key_env: "OPENAI_API_KEY"

Design
------
  * The function `transcribe_audio` returns a `TranscriptionResult`
    dataclass with `text`, `language`, `segments` (timestamped chunks).
  * DashScope requires a public URL for the file-trans model. For local
    files we upload via `dashscope.Files.upload()` first (auto-cleanup).
  * Results are cache-friendly — callers can wrap with AICache.
  * Long audio splitting is NOT this module's job — the caller
    (media_parser) handles segmentation via ffmpeg before calling us.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Providers that route to DashScope SDK instead of litellm.
# Extracted as a module constant so the routing decision is visible and
# maintainable in one place (not buried inside an if-chain).
_DASHSCOPE_PROVIDERS = {"dashscope", "qwen", "alibaba"}


@dataclass
class TranscriptionSegment:
    """A timestamped segment of transcribed text."""
    start: float   # seconds
    end: float     # seconds
    text: str


@dataclass
class TranscriptionResult:
    """Result from an audio transcription call."""
    text: str                                        # full transcript
    language: str = ""                               # detected language code
    segments: list[TranscriptionSegment] = field(default_factory=list)
    engine: str = ""                                 # which provider was used
    error: str = ""                                  # non-empty if failed


def _set_api_key(model_entry: dict[str, Any]) -> str | None:
    """
    Resolve the API key for a model entry. Returns the key or None.

    Priority (first non-empty wins):
      1. Plaintext `api_key` in the entry — lets downstream callers inject
         credentials via the Provider class (providers.py) without touching
         .env. Returned directly AND written to the matching env var so
         litellm-based fallback transcription (Whisper) also picks it up.
      2. `api_key_env` pointing at an existing env var — classic path,
         untouched for backwards compatibility.
    """
    explicit = model_entry.get("api_key")
    if explicit:
        # Mirror provider.py's behaviour: populate the matching env var so
        # _transcribe_litellm paths that rely on litellm's env-var auth
        # also see the key. The DashScope path uses the returned value
        # directly via dashscope.api_key = api_key.
        env_key = model_entry.get("api_key_env")
        if not env_key:
            from .provider import _PROVIDER_TO_ENV_KEY
            provider = str(model_entry.get("provider", "")).lower()
            env_key = _PROVIDER_TO_ENV_KEY.get(provider)
        if env_key:
            os.environ[env_key] = explicit
        return explicit

    env_key = model_entry.get("api_key_env")
    if env_key:
        return os.environ.get(env_key)
    return None


# ---------------------------------------------------------------------------
# DashScope provider (MultiModalConversation + base64, aligned with qwen_asr.py)
# ---------------------------------------------------------------------------

def _transcribe_dashscope(
    audio_path: Path,
    model: str,
    api_key: str,
    language: str | None = None,
    hotwords: str | None = None,
    enable_timestamps: bool = True,
) -> TranscriptionResult:
    """
    Transcribe via DashScope MultiModalConversation (qwen3-asr-flash).

    Architecture aligned with QwenASR/qwen_asr.py:
      * Audio is base64-encoded and sent as a multimodal message
        (same pattern as Vision sending images).
      * No file upload step needed — simpler and more reliable.
      * Model: qwen3-asr-flash (real-time, max ~3min per segment).
      * For long audio, the caller (media_parser) splits into segments
        first, then calls us per segment.

    Uses the DashScope international endpoint by default.
    """
    import base64
    import dashscope
    from dashscope import MultiModalConversation

    _ = enable_timestamps  # timestamps come from segment positions, not API
    dashscope.api_key = api_key
    # International endpoint (same as qwen_asr.py)
    dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"

    # Base64-encode the audio file
    audio_bytes = audio_path.read_bytes()
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    # Detect mime from extension
    ext = audio_path.suffix.lower()
    mime_map = {".mp3": "audio/mp3", ".wav": "audio/wav", ".m4a": "audio/m4a",
                ".flac": "audio/flac", ".ogg": "audio/ogg", ".aac": "audio/aac"}
    mime = mime_map.get(ext, "audio/mp3")

    # Build multimodal messages (same structure as qwen_asr.py)
    messages = [
        {"role": "system", "content": [{"text": hotwords or ""}]},
        {"role": "user", "content": [{"audio": f"data:{mime};base64,{audio_b64}"}]},
    ]

    # ASR options
    asr_options: dict[str, Any] = {"enable_lid": True}
    if language:
        asr_options["language"] = language

    try:
        response = MultiModalConversation.call(
            api_key=api_key,
            model=model,
            messages=messages,
            result_format="message",
            asr_options=asr_options,
        )

        if response.status_code != 200:
            raise RuntimeError(
                f"DashScope API error: {response.code} - {response.message}"
            )

        # Extract text from response
        message = response.output.choices[0].message
        text = message.content[0]["text"]

        # Extract language + emotion from annotations
        detected_lang = ""
        if hasattr(message, "annotations") and message.annotations:
            for ann in message.annotations:
                if ann.get("type") == "audio_info":
                    detected_lang = ann.get("language", "")

        return TranscriptionResult(
            text=text.strip(),
            language=detected_lang or (language or ""),
            engine=f"dashscope/{model}",
        )

    except Exception as e:
        raise RuntimeError(f"DashScope ASR failed: {e}") from e


# ---------------------------------------------------------------------------
# litellm provider (OpenAI Whisper / Groq / Azure / etc.)
# ---------------------------------------------------------------------------

def _transcribe_litellm(
    audio_path: Path,
    provider: str,
    model: str,
    api_key: str | None = None,
    language: str | None = None,
    num_retries: int = 2,
) -> TranscriptionResult:
    """
    Transcribe via litellm.transcription() (OpenAI-compatible providers).

    num_retries is threaded through to litellm for network-level retry on
    transient failures. Whether litellm.transcription honours the kwarg
    depends on the installed version (it travels via **kwargs like other
    litellm callables), so this is best-effort rather than guaranteed.
    Passing the kwarg is always safe — unrecognised kwargs are ignored.
    """
    import litellm
    from .provider import _resolve_model_name

    _ = api_key  # litellm reads keys from env vars; param kept for signature consistency
    model_str = _resolve_model_name(provider, model)

    try:
        with open(audio_path, "rb") as f:
            response = litellm.transcription(
                model=model_str,
                file=f,
                language=language,
                num_retries=num_retries,
            )

        # litellm returns an OpenAI-compatible TranscriptionResponse
        text = response.text if hasattr(response, "text") else str(response)

        # litellm doesn't return segments by default (need response_format=verbose_json)
        # Keep it simple for now — no segments.
        return TranscriptionResult(
            text=text.strip(),
            language=language or "",
            engine=f"{provider}/{model}",
        )
    except Exception as e:
        raise RuntimeError(f"litellm transcription failed: {e}") from e


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def transcribe_audio(
    audio_path: Path,
    model_config: dict[str, Any] | None = None,
    language: str | None = None,
    hotwords: str | None = None,
    enable_timestamps: bool = True,
) -> TranscriptionResult:
    """
    Transcribe an audio file using the configured engine chain.

    Tries primary provider, falls back to fallback on failure.
    Mirrors the architecture of describe_image / text_completion.

    Args:
        audio_path: Path to local audio file.
        model_config: Config dict with 'primary' and optional 'fallback'.
        language: Optional language hint (e.g. "ja", "en", "zh").
        hotwords: Optional context/hotwords for better recognition.
        enable_timestamps: Request word-level timestamps (DashScope).

    Returns:
        TranscriptionResult with text, optional segments, detected language.

    Raises:
        RuntimeError if all providers fail.
    """
    from .provider import _build_model_chain, resolve_max_retries

    models_to_try = _build_model_chain(model_config)
    # Network-level retries for the litellm path. The DashScope path uses its
    # own SDK, which manages retries internally — we pass num_retries only to
    # _transcribe_litellm, not _transcribe_dashscope.
    num_retries = resolve_max_retries(model_config)
    last_error: Exception | None = None

    for model_entry in models_to_try:
        provider = model_entry.get("provider", "openai").lower()
        model = model_entry.get("model", "whisper-1")
        api_key = _set_api_key(model_entry)

        if not api_key:
            logger.debug(
                f"No API key for {provider}/{model} "
                f"(env: {model_entry.get('api_key_env', '?')}), skipping"
            )
            continue

        try:
            if provider in _DASHSCOPE_PROVIDERS:
                return _transcribe_dashscope(
                    audio_path, model, api_key,
                    language=language,
                    hotwords=hotwords,
                    enable_timestamps=enable_timestamps,
                )
            else:
                return _transcribe_litellm(
                    audio_path, provider, model,
                    api_key=api_key,
                    language=language,
                    num_retries=num_retries,
                )
        except Exception as e:
            logger.warning(f"ASR failed with {provider}/{model}: {e}")
            last_error = e
            continue

    raise RuntimeError(
        f"Audio transcription failed for {audio_path.name}. "
        f"Last error: {last_error}"
    )
