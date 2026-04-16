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
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class _ModelUsage:
    """Accumulated usage for a single model."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    cache_hits: int = 0


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
    ) -> None:
        """Record tokens from one API call."""
        with self._lock:
            entry = self._data[model]
            entry.prompt_tokens += prompt
            entry.completion_tokens += completion
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

            for model, usage in sorted(self._data.items()):
                model_total = usage.prompt_tokens + usage.completion_tokens
                by_model[model] = {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": model_total,
                    "calls": usage.calls,
                    "cache_hits": usage.cache_hits,
                }
                total_prompt += usage.prompt_tokens
                total_completion += usage.completion_tokens
                total_calls += usage.calls
                total_cache_hits += usage.cache_hits

            return {
                "total_prompt_tokens": total_prompt,
                "total_completion_tokens": total_completion,
                "total_tokens": total_prompt + total_completion,
                "total_calls": total_calls,
                "total_cache_hits": total_cache_hits,
                "by_model": by_model,
            }

    def reset(self) -> None:
        """Clear all accumulated data."""
        with self._lock:
            self._data.clear()


# Process-level singleton
token_tracker = TokenTracker()
