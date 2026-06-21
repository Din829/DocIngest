"""
Load a knowledge base's produced artefacts as post-processing input.

Two input modes (config: postprocess.input):
  * "sources" (default) — one unit per sources/*.md file, frontmatter
    stripped. Best when the processor needs whole-document context
    (extraction of a per-document record).
  * "chunks"            — one unit per chunks.jsonl line. Best for
    fine-grained per-chunk processing.

A "unit" is the atom a PostProcessor works on. The Runner decides whether a
unit is further split (when it exceeds the model's context budget) — this
loader only reads + yields; it never calls an LLM and never mutates the
source files (they are shared with RAG / agentic-search consumers).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..config import get_nested


@dataclass
class SourceUnit:
    """One post-processing input unit.

    unit_id  — stable identifier (md filename stem, or chunk id). Used as the
               cache key and to label outputs.
    text     — the content to process (frontmatter already stripped for md).
    source   — relative path of the originating artefact, for attribution.
    metadata — any structured metadata available (md frontmatter / chunk
               metadata). Passed through to outputs; never fed to the LLM
               unless a processor chooses to.
    """

    unit_id: str
    text: str
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _strip_frontmatter(md: str) -> tuple[str, dict[str, Any]]:
    """Split leading YAML frontmatter from a sources/*.md body.

    Mirrors the heuristic api._parse_frontmatter / pipeline use: a block
    delimited by a leading ``---\\n`` and a closing ``\\n---\\n``. Returns
    (body, frontmatter_dict). No frontmatter → (original, {})."""
    if not md.startswith("---\n"):
        return md, {}
    end = md.find("\n---\n", 4)
    if end == -1:
        return md, {}
    body = md[end + 5:]
    meta: dict[str, Any] = {}
    try:
        import yaml
        parsed = yaml.safe_load(md[4:end])
        if isinstance(parsed, dict):
            meta = parsed
    except Exception:
        meta = {}
    return body, meta


def load_units(
    knowledge_dir: Path,
    config: dict[str, Any],
) -> Iterator[SourceUnit]:
    """
    Yield SourceUnit records from a knowledge base, per postprocess.input.

    Reads are streaming where it matters (chunks.jsonl can be 100K+ lines).
    A missing artefact yields nothing rather than raising — the caller
    reports "0 units" which is the honest signal that the KB wasn't built
    or the input mode is wrong.
    """
    mode = str(get_nested(config, "postprocess.input", "sources")).lower()

    if mode == "chunks":
        yield from _load_chunk_units(knowledge_dir, config)
    else:
        yield from _load_md_units(knowledge_dir, config)


def _load_md_units(
    knowledge_dir: Path,
    config: dict[str, Any],
) -> Iterator[SourceUnit]:
    sources_dir_name = get_nested(config, "output.sources_dir", "sources")
    sources_dir = knowledge_dir / sources_dir_name
    if not sources_dir.is_dir():
        return
    for md_path in sorted(sources_dir.glob("*.md")):
        try:
            raw = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        body, meta = _strip_frontmatter(raw)
        rel = md_path.relative_to(knowledge_dir).as_posix()
        yield SourceUnit(
            unit_id=md_path.stem,
            text=body,
            source=rel,
            metadata=meta,
        )


def _load_chunk_units(
    knowledge_dir: Path,
    config: dict[str, Any],
) -> Iterator[SourceUnit]:
    chunks_file = get_nested(config, "chunking.output_file", "chunks.jsonl")
    chunks_path = knowledge_dir / chunks_file
    if not chunks_path.exists():
        return
    min_tokens = int(get_nested(config, "postprocess.min_chunk_tokens", 0) or 0)
    try:
        with open(chunks_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                meta = rec.get("metadata") or {}
                if min_tokens and isinstance(meta, dict):
                    if int(meta.get("tokens", 0) or 0) < min_tokens:
                        continue
                yield SourceUnit(
                    unit_id=str(rec.get("id", "")),
                    text=str(rec.get("text", "")),
                    source=str(meta.get("source", "")) if isinstance(meta, dict) else "",
                    metadata=meta if isinstance(meta, dict) else {},
                )
    except OSError:
        return
