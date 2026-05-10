"""
DocIngest provider → LightRAG ``llm_model_func`` adapter.

LightRAG calls the LLM through an async callable with this exact shape
(verified against HKUDS/LightRAG ProgramingWithCore.md):

    async def llm_model_func(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict] = [],
        keyword_extraction: bool = False,
        **kwargs,
    ) -> str:
        return response_string

DocIngest's provider layer (models/provider.py::text_completion) is sync,
returns ``(content, finish_reason)``, and already implements:
    - primary → fallback chain
    - litellm network-level retries
    - finish_reason="length" truncation retry
    - token tracking via models.token_tracker

This adapter wraps that machinery into the LightRAG-shaped async callable.
We delegate everything; we add nothing on top. The single subtlety is the
``history_messages`` parameter: LightRAG sometimes passes prior turns to
support multi-turn queries. We translate them into a single concatenated
prompt (text_completion has no history concept), preserving role hints in
plain text. This is faithful to LightRAG's own OpenAI-binding fallback for
backends that don't natively support chat history.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from ...models.provider import text_completion


# Type alias for the callable LightRAG expects. We don't import LightRAG's
# own typing here — the function is duck-typed by LightRAG (it just calls
# it), and keeping the import out of the adapter's import path means this
# module loads even when lightrag-hku isn't installed (useful for tests).
LightRAGLLMFunc = Callable[..., Any]


def _format_history(history_messages: list[dict] | None) -> str:
    """
    Render LightRAG's history list as a small "Prior conversation" block.

    LightRAG passes history as ``[{"role": "user"|"assistant", "content": "..."}]``.
    We render compactly so it costs minimal context tokens; long histories
    are the caller's problem (LightRAG handles its own context budgeting).
    """
    if not history_messages:
        return ""
    lines: list[str] = ["Prior conversation:"]
    for msg in history_messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user")).strip() or "user"
        content = str(msg.get("content", "")).strip()
        if not content:
            continue
        lines.append(f"  [{role}] {content}")
    return "\n".join(lines) + "\n\n"


def make_lightrag_llm_func(model_config: dict[str, Any]) -> LightRAGLLMFunc:
    """
    Build a LightRAG-compatible async LLM function backed by DocIngest's
    text_completion machinery.

    Args:
        model_config: A model config dict shaped like ``models.<task>`` —
            ``primary`` / optional ``fallback`` / ``max_response_tokens`` /
            ``max_retries``, plus an ``_defaults`` subdict that load_config
            injects. The config in ``graph.llm`` matches this shape exactly,
            so the caller passes ``config["graph"]["llm"]`` (after
            ``_inject_model_defaults`` has run, see config.py).

    Returns:
        An async callable matching LightRAG's expected llm_model_func
        signature. LightRAG owns the lifetime; we hold no state.

    Notes on retry / fallback semantics:
        text_completion already returns the FINAL response after retrying
        truncation and walking the primary→fallback chain. The adapter
        therefore does NOT add its own retry logic — doing so would
        duplicate (and conflict with) the layers already in place.
    """

    async def llm_model_func(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict] | None = None,
        keyword_extraction: bool = False,
        **kwargs: Any,
    ) -> str:
        # keyword_extraction / kwargs are part of LightRAG's documented
        # signature — accept them so duck-typing succeeds, ignore values
        # we don't need (text_completion has no equivalent knob).
        del keyword_extraction, kwargs
        # Combine history + main prompt. We could pass history_messages
        # through to litellm verbatim, but text_completion's contract is
        # single-prompt — keeping the simple shape avoids leaking yet
        # another path through provider.py.
        history_block = _format_history(history_messages)
        final_prompt = (history_block + prompt) if history_block else prompt

        # text_completion is synchronous and may take seconds (LLM call).
        # Run on a worker thread so we don't block LightRAG's event loop.
        # The system_prompt ("" sentinel matches text_completion's default).
        content, _finish_reason = await asyncio.to_thread(
            text_completion,
            prompt=final_prompt,
            system_prompt=system_prompt or "",
            model_config=model_config,
            # Both retry layers (network + truncation) are already configured
            # by load_config / models.defaults. Don't override here.
        )
        return content

    return llm_model_func


__all__ = ["make_lightrag_llm_func"]
