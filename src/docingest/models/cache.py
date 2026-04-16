"""
AI call result cache.

Caches Vision descriptions and text completions to avoid paying twice
for the same input. Cache key includes model ID + content hash + config hash
so results auto-invalidate when inputs or settings change.

Uses diskcache for persistent storage (survives process restarts).
Falls back to in-memory dict if diskcache is not available.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

try:
    import diskcache
    _HAS_DISKCACHE = True
except ImportError:
    _HAS_DISKCACHE = False

from .token_tracker import token_tracker


def _make_key(model_name: str, content_hash: str, extra: str = "") -> str:
    """Build a deterministic cache key."""
    raw = f"{model_name}|{content_hash}|{extra}"
    return hashlib.sha256(raw.encode()).hexdigest()


def content_hash_file(file_path: Path) -> str:
    """Compute SHA256 hash of a file's contents."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash_bytes(data: bytes) -> str:
    """Compute SHA256 hash of bytes."""
    return hashlib.sha256(data).hexdigest()


class AICache:
    """
    Persistent cache for AI API call results.

    Usage:
        cache = AICache(cache_dir=".docingest_cache")
        result = cache.get_or_call(
            model_name="gemini/gemini-3-flash",
            content_hash="abc123...",
            call_fn=lambda: describe_image(img_path, prompt, config),
        )
    """

    def __init__(self, cache_dir: str | Path = ".docingest_cache", enabled: bool = True) -> None:
        self.enabled = enabled

        if enabled and _HAS_DISKCACHE:
            self._disk = diskcache.Cache(str(cache_dir))
        else:
            self._disk = None

        # In-memory fallback (per-process only)
        self._memory: dict[str, str] = {}

    def get_or_call(
        self,
        model_name: str,
        content_hash: str,
        call_fn: Callable[[], str],
        extra_key: str = "",
    ) -> str:
        """
        Get cached result or call the function and cache the result.

        Args:
            model_name: Model identifier (for cache key).
            content_hash: Hash of the input content (for cache key).
            call_fn: Function to call if cache miss. Must return str.
            extra_key: Additional key component (e.g., prompt hash).

        Returns:
            Cached or freshly computed result string.
        """
        if not self.enabled:
            return call_fn()

        key = _make_key(model_name, content_hash, extra_key)

        # Check disk cache
        if self._disk is not None:
            cached = self._disk.get(key)
            if cached is not None:
                token_tracker.record_cache_hit(model_name)
                return cached

        # Check memory cache
        if key in self._memory:
            token_tracker.record_cache_hit(model_name)
            return self._memory[key]

        # Cache miss → call function
        result = call_fn()

        # Store in both caches
        if self._disk is not None:
            self._disk.set(key, result)
        self._memory[key] = result

        return result

    def close(self) -> None:
        """Close disk cache (release file locks)."""
        if self._disk is not None:
            self._disk.close()
