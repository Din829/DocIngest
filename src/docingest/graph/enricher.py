"""
Chunk enrichment — feed graph entities back into a parallel chunks file.

Goal:
    Make traditional vector RAG benefit from what GraphRAG already
    extracted. The original ``chunks.jsonl`` is NEVER modified — we read
    the graph artefacts under ``graph/`` plus ``chunks.jsonl``, then
    write a brand-new ``chunks_enriched.jsonl`` next to it. Users who
    want the boost vectorise that file instead.

Data flow (entirely from on-disk graph products — no LLM calls):

    graph/vdb_entities.json
       ├── data: [{entity_name, content, source_id, file_path}, ...]
       └── source_id is "<SEP>"-joined LightRAG chunk ids
                                   │
                                   ▼ resolve via
    graph/kv_store_text_chunks.json
       └── { lightrag_id: {full_doc_id: <docingest chunk_id>, ...} }
                                   │
                                   ▼ build inverted index
        { docingest_chunk_id: [(entity_name, description, exclusive?), ...] }
                                   │
                                   ▼ stream-rewrite
    chunks.jsonl  ─►  chunks_enriched.jsonl
       (read-only)       (text + metadata both gain entities)

Design constraints kept:

* Original chunks.jsonl byte-for-byte unchanged (verified by tests).
* Main pipeline's incremental.py never sees this — chunks.jsonl is the
  hash input it cares about, and we don't touch that.
* Failure-tolerant: any error short-circuits cleanly with a structured
  error report, never raises out of the public surface. graph build
  itself shouldn't be reported as failed because enrichment hiccupped.
* No LLM / embedding calls in this module — it's a pure replay of
  what the graph build already produced.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ..config import get_nested

logger = logging.getLogger(__name__)


# LightRAG's documented intra-field separator for joining multiple values
# (multiple source_ids on one entity, multiple description versions on one
# merged entity). Confirmed by inspecting kb output and LightRAG source.
_LIGHTRAG_SEP = "<SEP>"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EnrichResult:
    """Returned by ``enrich``; surfaced into BuildResult.stats too."""

    written_path: str = ""
    chunks_total: int = 0
    chunks_enriched: int = 0          # had >= 1 entity attached
    chunks_unchanged: int = 0         # zero entities found, copied as-is
    total_entities_injected: int = 0
    avg_entities_per_chunk: float = 0.0
    elapsed_ms: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _EntityHit:
    """Internal row in the per-chunk inverted index."""

    name: str
    description: str
    exclusive: bool   # True iff this entity's source_id contains ONLY
                      # the chunk we're attributing it to (high-signal).


# ---------------------------------------------------------------------------
# Loaders — pure file readers
# ---------------------------------------------------------------------------

def _load_lightrag_id_map(graph_dir: Path) -> dict[str, str]:
    """
    Build {lightrag_chunk_id -> docingest_chunk_id} from the LightRAG
    text-chunks store. Returns empty dict on any read failure (caller
    treats that as "nothing to enrich" and writes a passthrough copy).
    """
    path = graph_dir / "kv_store_text_chunks.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"failed to read {path.name}: {e}")
        return {}
    if not isinstance(data, dict):
        return {}

    mapping: dict[str, str] = {}
    for lightrag_id, record in data.items():
        if not isinstance(record, dict):
            continue
        full_doc_id = record.get("full_doc_id")
        if isinstance(full_doc_id, str) and full_doc_id:
            mapping[str(lightrag_id)] = full_doc_id
    return mapping


def _split_lightrag_field(value: str) -> list[str]:
    """Split a LightRAG-joined field on its <SEP> separator, dropping empties."""
    if not value:
        return []
    return [p.strip() for p in value.split(_LIGHTRAG_SEP) if p.strip()]


def _parse_entity_content(content: str) -> tuple[str, str]:
    """
    LightRAG stores ``content = entity_name + "\\n" + description``. When
    descriptions have been merged across runs they're joined with <SEP>;
    we keep the first segment (most recent / usually richest).
    """
    if not content:
        return "", ""
    if "\n" not in content:
        return content.strip(), ""
    head, tail = content.split("\n", 1)
    description_segments = _split_lightrag_field(tail)
    description = description_segments[0] if description_segments else tail.strip()
    return head.strip(), description


def _load_entities(graph_dir: Path) -> list[dict[str, Any]]:
    """
    Load vdb_entities.json's data list. Returns [] on any failure — the
    caller treats that as "no entities to inject", writing a passthrough
    enriched copy of chunks.jsonl.
    """
    path = graph_dir / "vdb_entities.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"failed to read {path.name}: {e}")
        return []
    entries = data.get("data") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


# ---------------------------------------------------------------------------
# Inverted index: chunk_id -> [_EntityHit]
# ---------------------------------------------------------------------------

def _build_inverted_index(
    entities: list[dict[str, Any]],
    lightrag_to_docingest: dict[str, str],
) -> dict[str, list[_EntityHit]]:
    """
    For every entity, walk its source_id list and append a hit to each
    target chunk's bucket. Entities whose source_id resolves to exactly
    one chunk are flagged ``exclusive=True`` — they're more definitional
    and get prioritised at injection time.

    Hits are deduplicated per (chunk_id, entity_name) so an entity that
    accidentally lists a chunk twice in source_id doesn't double-count.
    """
    index: dict[str, list[_EntityHit]] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = entity.get("entity_name", "")
        content = entity.get("content", "")
        source_id_raw = entity.get("source_id", "")
        if not name or not source_id_raw:
            continue

        lightrag_ids = _split_lightrag_field(source_id_raw)
        if not lightrag_ids:
            continue

        # Resolve LightRAG ids → our chunk ids. Skip silently if mapping
        # missing (orphaned entities exist when graph was built against
        # a different chunks.jsonl than the one we're enriching).
        doc_ids = [
            lightrag_to_docingest[lid]
            for lid in lightrag_ids
            if lid in lightrag_to_docingest
        ]
        if not doc_ids:
            continue

        _entity_name, description = _parse_entity_content(content)
        # Prefer the entity_name field as written (case-stable) over what
        # we'd parse out of content — they should match, but the explicit
        # field is canonical.
        canonical_name = name.strip()

        exclusive = len(set(doc_ids)) == 1

        for doc_id in doc_ids:
            pair = (doc_id, canonical_name)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            index.setdefault(doc_id, []).append(
                _EntityHit(
                    name=canonical_name,
                    description=description,
                    exclusive=exclusive,
                )
            )

    return index


# ---------------------------------------------------------------------------
# Top-N selection per chunk
# ---------------------------------------------------------------------------

def _select_top_entities(
    hits: list[_EntityHit],
    max_entities: int,
) -> list[_EntityHit]:
    """
    Sort hits and pick the top N. Sort key:
        1. exclusive entities first (higher signal — defined by this chunk)
        2. shorter names first (longer names tend to be doc-level boilerplate
           like the full file title repeated as an entity)
    Ties broken alphabetically for determinism — same input always produces
    the same enriched output, which the idempotency test verifies.
    """
    if not hits:
        return []
    if max_entities <= 0:
        return []

    sorted_hits = sorted(
        hits,
        key=lambda h: (
            0 if h.exclusive else 1,
            len(h.name),
            h.name,
        ),
    )
    return sorted_hits[:max_entities]


# ---------------------------------------------------------------------------
# Stream rewrite
# ---------------------------------------------------------------------------

def _truncate_description(desc: str, max_len: int) -> str:
    """Hard-cut at max_len with ellipsis. Empty / short strings pass through."""
    if not desc:
        return ""
    if max_len <= 0 or len(desc) <= max_len:
        return desc
    return desc[: max_len - 1].rstrip() + "…"


def _format_text_injection(
    selected: list[_EntityHit],
    *,
    template: str,
    entity_separator: str,
    name_desc_separator: str,
    max_description_length: int,
) -> str:
    """
    Build the single-line "[关键实体: A — desc; B — desc]" block. Returns
    empty string when there's nothing to inject (caller skips the line).
    """
    if not selected:
        return ""
    parts: list[str] = []
    for hit in selected:
        desc = _truncate_description(hit.description, max_description_length)
        if desc:
            parts.append(f"{hit.name}{name_desc_separator}{desc}")
        else:
            # Entity with no description — still useful as a keyword anchor.
            parts.append(hit.name)
    body = entity_separator.join(parts)
    return template.format(entities=body)


def _inject_text_after_path_header(text: str, injection: str) -> str:
    """
    Place the injection line right after the main pipeline's path header
    (the "[来源: ...]" / "[Source: ...]" block, if present), otherwise at
    the very top. This keeps the embedding model's high-weight prefix
    region focused on the most informative metadata.

    Idempotent on a re-run: if the same injection line is already present
    immediately after the path header we leave the text alone. Detection
    is done on the literal line, not on entity content, so changing the
    entity set DOES regenerate (the caller intends that).
    """
    if not injection:
        return text

    # Find the first newline after a leading "[来源:" / "[Source:" header.
    has_path_header = (
        text.startswith("[来源:") or text.startswith("[Source:")
    )

    if has_path_header:
        nl = text.find("\n")
        if nl == -1:
            # Pathological: header but no newline — append + newline.
            return text + "\n" + injection + "\n"
        head = text[: nl + 1]
        rest = text[nl + 1 :]
        if rest.startswith(injection + "\n") or rest.startswith(injection):
            return text  # already injected
        return head + injection + "\n" + rest

    # No path header → put the injection at the very top.
    if text.startswith(injection + "\n") or text.startswith(injection):
        return text
    return injection + "\n" + text


def _strip_existing_injection(text: str, template: str) -> str:
    """
    On re-runs we want the new entity set to replace the old one rather
    than stack. Locate the line matching the template's literal prefix
    (e.g. "[关键实体:") inside the first 3 lines and strip it.

    The template is config-driven so we extract its prefix dynamically.
    """
    # Static prefix = template up to the "{entities}" placeholder. This
    # is what we put in to find any prior injection we made ourselves.
    placeholder = "{entities}"
    idx = template.find(placeholder)
    if idx <= 0:
        return text
    prefix = template[:idx]
    if not prefix:
        return text

    lines = text.split("\n", 3)  # check up to first 3 lines
    new_lines: list[str] = []
    removed = False
    for i, line in enumerate(lines[:3]):
        if not removed and line.lstrip().startswith(prefix):
            removed = True
            continue
        new_lines.append(line)
    if not removed:
        return text
    # Re-attach the rest (anything past the first 3 lines).
    if len(lines) == 4:
        new_lines.append(lines[3])
    return "\n".join(new_lines)


def _iter_input_chunks(chunks_path: Path) -> Iterator[dict[str, Any]]:
    """Stream chunks.jsonl — malformed lines are skipped (mirrors chunks_loader)."""
    with open(chunks_path, "r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


# ---------------------------------------------------------------------------
# Public entry — the only function api.py / cli.py need
# ---------------------------------------------------------------------------

def enrich(output_dir: Path, config: dict[str, Any]) -> EnrichResult:
    """
    Generate ``chunks_enriched.jsonl`` from existing graph artefacts.

    Args:
        output_dir: Knowledge-base root (the same path you passed to
            ``docingest run`` / ``docingest graph build``).
        config: Fully-merged DocIngest config. The relevant section is
            ``graph.enrich_chunks.*`` plus a few inputs from the broader
            ``graph.*`` for path resolution.

    Returns:
        EnrichResult with counts, elapsed time, and any soft errors. The
        function NEVER raises — graph build wouldn't want a follow-up
        bug to mark the whole build as failed. Hard preconditions failing
        (no graph, no chunks) surface as descriptive errors in the
        returned object.
    """
    t_start = time.monotonic()
    result = EnrichResult()

    # Resolve paths
    graph_subdir = get_nested(config, "graph.output_subdir", "graph")
    graph_dir = output_dir / graph_subdir
    chunks_path = output_dir / get_nested(
        config, "graph.input.chunks_file", "chunks.jsonl"
    )
    enrich_cfg = get_nested(config, "graph.enrich_chunks", {}) or {}
    out_filename = enrich_cfg.get("output_file", "chunks_enriched.jsonl")
    out_path = output_dir / out_filename

    if not chunks_path.exists():
        result.errors.append(
            f"chunks file not found: {chunks_path} — run `docingest run` first"
        )
        result.elapsed_ms = int((time.monotonic() - t_start) * 1000)
        return result

    if not graph_dir.exists():
        result.errors.append(
            f"graph dir not found: {graph_dir} — run `docingest graph build` first"
        )
        result.elapsed_ms = int((time.monotonic() - t_start) * 1000)
        return result

    # Load graph products
    lightrag_to_docingest = _load_lightrag_id_map(graph_dir)
    entities = _load_entities(graph_dir)

    if not entities or not lightrag_to_docingest:
        # Graph dir exists but is empty / corrupt — write a passthrough copy
        # so downstream code can always assume the file is there. Users can
        # tell the difference by counting injected entities (will be 0).
        result.errors.append(
            "graph artefacts incomplete (no entities or no chunk-id map) — "
            "writing passthrough copy without enrichment"
        )

    inverted = _build_inverted_index(entities, lightrag_to_docingest)

    # Read enrichment knobs once
    inject_into_text = bool(enrich_cfg.get("inject_into_text", True))
    inject_into_metadata = bool(enrich_cfg.get("inject_into_metadata", True))
    max_entities = int(enrich_cfg.get("max_entities_per_chunk", 5))
    max_desc_len = int(enrich_cfg.get("max_description_length", 100))
    text_template = str(enrich_cfg.get("text_template", "[关键实体: {entities}]"))
    entity_sep = str(enrich_cfg.get("entity_separator", "; "))
    name_desc_sep = str(enrich_cfg.get("name_desc_separator", " — "))

    timestamp_marker = (
        "graph_build_" + datetime.now().isoformat(timespec="seconds")
    )

    # Stream rewrite — atomic via .tmp + os.replace
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_entities = 0
    try:
        with open(tmp_path, "w", encoding="utf-8") as out_f:
            for record in _iter_input_chunks(chunks_path):
                result.chunks_total += 1

                chunk_id = record.get("id")
                text = record.get("text", "")
                metadata = record.get("metadata") or {}
                if not isinstance(metadata, dict):
                    metadata = {}

                hits = inverted.get(str(chunk_id), []) if chunk_id else []
                selected = _select_top_entities(hits, max_entities)

                if selected:
                    result.chunks_enriched += 1
                    total_entities += len(selected)
                else:
                    result.chunks_unchanged += 1

                # Build the new record. Don't mutate the original dict — keep
                # the original chunks.jsonl byte-stable even in pathological
                # caller patterns.
                new_metadata = dict(metadata)

                if inject_into_metadata and selected:
                    new_metadata["entities"] = [
                        {
                            "name": h.name,
                            "description": _truncate_description(
                                h.description, max_desc_len
                            ),
                            "exclusive": h.exclusive,
                        }
                        for h in selected
                    ]
                    new_metadata["enriched_from"] = timestamp_marker

                if inject_into_text and selected:
                    injection = _format_text_injection(
                        selected,
                        template=text_template,
                        entity_separator=entity_sep,
                        name_desc_separator=name_desc_sep,
                        max_description_length=max_desc_len,
                    )
                    # Strip any prior injection first so re-runs replace
                    # rather than stack.
                    cleaned = _strip_existing_injection(text, text_template)
                    new_text = _inject_text_after_path_header(cleaned, injection)
                else:
                    new_text = text

                new_record = {
                    "id": chunk_id,
                    "text": new_text,
                    "metadata": new_metadata,
                }
                out_f.write(
                    json.dumps(new_record, ensure_ascii=False) + "\n"
                )
    except OSError as e:
        result.errors.append(f"write failure: {e}")
        try:
            tmp_path.unlink()
        except OSError:
            pass
        result.elapsed_ms = int((time.monotonic() - t_start) * 1000)
        return result

    # Atomic publish
    try:
        import os
        os.replace(tmp_path, out_path)
    except OSError as e:
        result.errors.append(f"atomic rename failed: {e}")
        result.elapsed_ms = int((time.monotonic() - t_start) * 1000)
        return result

    result.written_path = str(out_path)
    result.total_entities_injected = total_entities
    if result.chunks_total:
        result.avg_entities_per_chunk = round(
            total_entities / result.chunks_total, 3
        )
    result.elapsed_ms = int((time.monotonic() - t_start) * 1000)
    return result


__all__ = ["enrich", "EnrichResult"]
