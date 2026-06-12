"""
Token usage tracker — lightweight, process-level accumulator.

Records prompt_tokens and completion_tokens from every LLM API call,
grouped by model name. Cache hits are counted separately (zero tokens).

Usage:
    from docingest.models.token_tracker import token_tracker

    # Record a call (provider.py does this automatically)
    token_tracker.record("gemini/gemini-3-flash", prompt=1200, completion=500)
    token_tracker.record_cache_hit("gemini/gemini-3-flash")

    # Get summary (pipeline.py does this at the end)
    summary = token_tracker.summary()
    token_tracker.reset()
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from threading import Lock


@dataclass
class _ModelUsage:
    """Accumulated usage for a single model."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    cache_hits: int = 0
    # Detail buckets — recorded VERBATIM from the API response, never
    # derived. Probed live (2026-06-12, gemini-3-flash-preview): litellm's
    # completion_tokens already INCLUDES reasoning (854 = 442 reasoning +
    # 412 text), so reasoning_tokens here is a breakdown of completion, not
    # an addition to it. cached_prompt_tokens is the provider-side
    # prompt-cache slice of prompt_tokens (billed cheaper). total_reported
    # accumulates the provider's own total_tokens figure — the audit
    # anchor: when it drifts from prompt+completion, an unbucketed cost
    # exists and the drift makes it visible. audio_seconds is the second
    # billing dimension for ASR calls (audio is priced per second/minute,
    # not only per token).
    reasoning_tokens: int = 0
    cached_prompt_tokens: int = 0
    total_reported: int = 0
    audio_seconds: float = 0.0


class TokenTracker:
    """Thread-safe, process-level token usage accumulator."""

    def __init__(self) -> None:
        self._data: dict[str, _ModelUsage] = defaultdict(_ModelUsage)
        self._lock = Lock()

    def record(
        self,
        model: str,
        prompt: int = 0,
        completion: int = 0,
        *,
        reasoning: int = 0,
        cached_prompt: int = 0,
        total_reported: int = 0,
        audio_seconds: float = 0.0,
    ) -> None:
        """Record one API call's usage, verbatim from the response.

        The keyword-only detail fields default to 0 so every existing call
        site keeps working; pass them when the response carries them."""
        with self._lock:
            entry = self._data[model]
            entry.prompt_tokens += prompt
            entry.completion_tokens += completion
            entry.reasoning_tokens += reasoning
            entry.cached_prompt_tokens += cached_prompt
            entry.total_reported += total_reported
            entry.audio_seconds += audio_seconds
            entry.calls += 1

    def record_cache_hit(self, model: str) -> None:
        """Record a cache hit (no tokens consumed)."""
        with self._lock:
            self._data[model].cache_hits += 1

    def summary(self) -> dict:
        """
        Return usage summary.

        Returns:
            {
                "total_prompt_tokens": int,
                "total_completion_tokens": int,
                "total_tokens": int,
                "total_calls": int,
                "total_cache_hits": int,
                "by_model": {
                    "gemini/gemini-3-flash": {
                        "prompt_tokens": int,
                        "completion_tokens": int,
                        "total_tokens": int,
                        "calls": int,
                        "cache_hits": int,
                    },
                    ...
                }
            }
        """
        with self._lock:
            by_model = {}
            total_prompt = 0
            total_completion = 0
            total_calls = 0
            total_cache_hits = 0
            total_reasoning = 0
            total_cached_prompt = 0
            total_reported = 0
            total_audio_seconds = 0.0

            for model, usage in sorted(self._data.items()):
                model_total = usage.prompt_tokens + usage.completion_tokens
                by_model[model] = {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": model_total,
                    "calls": usage.calls,
                    "cache_hits": usage.cache_hits,
                    "reasoning_tokens": usage.reasoning_tokens,
                    "cached_prompt_tokens": usage.cached_prompt_tokens,
                    "total_reported": usage.total_reported,
                    "audio_seconds": round(usage.audio_seconds, 1),
                }
                total_prompt += usage.prompt_tokens
                total_completion += usage.completion_tokens
                total_calls += usage.calls
                total_cache_hits += usage.cache_hits
                total_reasoning += usage.reasoning_tokens
                total_cached_prompt += usage.cached_prompt_tokens
                total_reported += usage.total_reported
                total_audio_seconds += usage.audio_seconds

            return {
                "total_prompt_tokens": total_prompt,
                "total_completion_tokens": total_completion,
                "total_tokens": total_prompt + total_completion,
                "total_calls": total_calls,
                "total_cache_hits": total_cache_hits,
                # Detail totals — verbatim sums of what providers reported.
                # total_reported_tokens is the audit anchor: a drift from
                # total_tokens (the prompt+completion sum above) means some
                # provider reported buckets we don't decompose.
                "total_reasoning_tokens": total_reasoning,
                "total_cached_prompt_tokens": total_cached_prompt,
                "total_reported_tokens": total_reported,
                "total_audio_seconds": round(total_audio_seconds, 1),
                "by_model": by_model,
            }

    def reset(self) -> None:
        """Clear all accumulated data."""
        with self._lock:
            self._data.clear()


# Process-level singleton
token_tracker = TokenTracker()
