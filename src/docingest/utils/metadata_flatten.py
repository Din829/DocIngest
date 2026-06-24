"""
Flatten chunk metadata for strict-schema vector stores.

Why this exists
---------------
DocIngest's ``chunks.jsonl`` carries a few NESTED metadata fields by design —
``lineage`` (a dict with ``original_input`` + ``transformations``) and the
per-format visual signals (``docx_visual_signals`` / ``xlsx_sheet_visuals``,
plain dicts). Qdrant and the built-in Azure exporter handle these fine (Qdrant
stores nested JSON natively; the Azure exporter only pushes the flat fields it
maps). But the strict-schema vector stores reject nested values outright.

This was VERIFIED against a real Pinecone serverless index (June 2026): a real
DocIngest chunk upserted as-is returns

    [400] Metadata value must be a string, number, boolean or list of strings,
          got '{"original_input...' for field 'lineage'

while the same chunk with nested fields flattened/dropped upserts cleanly. So
the accepted metadata value types are exactly: ``str | number | bool |
list[str]``. That is the contract this function targets.

A second, subtler trap also confirmed in that run: ``lineage.original_input.
binary_hash`` is Docling's 64-bit content hash, emitted as a Python int that
can exceed 2**63 (observed: an 18-digit value). JSON has no int size limit, but
many downstreams (and JSON parsers in other languages) silently lose precision
or overflow on ints beyond 2**53 / 2**63. ``stringify_big_ints`` converts those
to strings so the value survives the trip intact.

Scope / design
--------------
This is a PURE, OPT-IN transform applied AFTER reading ``chunks.jsonl`` and
BEFORE handing metadata to a strict store. It does NOT touch the pipeline or
the on-disk ``chunks.jsonl`` — that file is unchanged, and every existing
consumer (Azure exporter, graph, LangChain loader) is unaffected. Use it only
on the path to a store that needs flat metadata::

    from docingest.utils.metadata_flatten import flatten_metadata
    flat = flatten_metadata(chunk["metadata"])   # ready for Pinecone, etc.

Nothing about WHICH fields are nested is hardcoded — the function inspects
values structurally, so it keeps working if a future field turns out nested.
"""

from __future__ import annotations

import json
from typing import Any

# Integers outside this magnitude are stringified when stringify_big_ints is on.
# 2**53 is the largest integer JavaScript / IEEE-754 double can represent
# exactly; many JSON consumers downstream of a vector store parse numbers as
# doubles, so anything beyond this silently loses precision. Docling's
# binary_hash (a 64-bit FNV-style hash) routinely exceeds it.
_SAFE_INT_MAX = 2 ** 53


def _is_flat_scalar(value: Any) -> bool:
    """True for values a strict store accepts directly: str / number / bool.

    bool is intentionally checked via isinstance(bool) BEFORE int elsewhere
    because bool is a subclass of int in Python; here we just accept all three.
    """
    return isinstance(value, (str, int, float, bool))


def _is_list_of_strings(value: Any) -> bool:
    """True for the one accepted collection type: a list whose items are all str."""
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def flatten_metadata(
    metadata: dict[str, Any],
    *,
    mode: str = "flatten",
    sep: str = ".",
    stringify_big_ints: bool = True,
) -> dict[str, Any]:
    """Flatten one chunk's metadata into strict-store-compatible values.

    Produces a dict whose every value is ``str | int | float | bool |
    list[str]`` — the value types Pinecone (and similar strict vector stores)
    accept. ``chunks.jsonl`` itself is never modified; call this on the
    metadata you read out, just before pushing.

    Args:
        metadata: A chunk's ``metadata`` dict (from ``chunks.jsonl``).
        mode:
            ``"flatten"`` (default) — nested dicts become dotted keys
                (``lineage.original_input.filename``), preserving all
                provenance; lists are kept when they are list-of-strings,
                otherwise JSON-encoded to a single string so no data is lost.
            ``"drop"`` — nested dict/list values are removed entirely, leaving
                only the already-flat scalar fields. Smallest output; loses
                lineage.
        sep: Separator for dotted keys in ``"flatten"`` mode.
        stringify_big_ints: Convert ints with absolute value > 2**53 to str
            (protects ``binary_hash`` and any other large counter from
            precision loss in double-based JSON consumers). Floats and
            in-range ints are left as numbers.

    Returns:
        A new flat dict. The input is not mutated.

    Raises:
        ValueError: if ``mode`` is not ``"flatten"`` or ``"drop"`` — a typo'd
            mode is a caller bug, not something to silently treat as a default.
    """
    if mode not in ("flatten", "drop"):
        raise ValueError(f"mode must be 'flatten' or 'drop', got {mode!r}")

    out: dict[str, Any] = {}

    def _coerce_scalar(value: Any) -> Any:
        """Apply the big-int rule to a scalar; pass everything else through."""
        # bool first — bool is an int subclass, and a bool is never "big".
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and stringify_big_ints and abs(value) > _SAFE_INT_MAX:
            return str(value)
        return value

    def _emit(key: str, value: Any) -> None:
        """Place one (already-decided-flat) value into out, coercing scalars."""
        out[key] = _coerce_scalar(value)

    def _walk(prefix: str, value: Any) -> None:
        # Flat scalar → emit as-is (with big-int coercion).
        if _is_flat_scalar(value):
            _emit(prefix, value)
            return

        # list[str] is an accepted type — keep it whole.
        if _is_list_of_strings(value):
            out[prefix] = list(value)
            return

        # Nested dict.
        if isinstance(value, dict):
            if mode == "drop":
                return
            for k, v in value.items():
                _walk(f"{prefix}{sep}{k}", v)
            return

        # Any other list (contains dicts / nested lists / mixed) — not an
        # accepted type. In flatten mode, JSON-encode to a string so the
        # information survives (e.g. lineage.transformations, a list of dicts).
        # In drop mode, omit it.
        if isinstance(value, list):
            if mode == "drop":
                return
            out[prefix] = json.dumps(value, ensure_ascii=False)
            return

        # Fallback for anything exotic (e.g. a tuple, a custom object that
        # slipped into metadata). In flatten mode keep it as a string rather
        # than drop silently; in drop mode omit. This is the only place we
        # "defend" against an unexpected type, and it's at the system edge
        # (about to hit an external API), so it's warranted.
        if mode == "drop":
            return
        out[prefix] = str(value)

    for key, value in metadata.items():
        _walk(key, value)

    return out


__all__ = ["flatten_metadata"]
