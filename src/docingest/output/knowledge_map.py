"""
Knowledge Map generator — produces a searchable guide for the knowledge base.

Two stages:
  Stage 1 (automatic, zero cost):
    - File listing with sections/sheets/slides
    - Keyword extraction from headings and title_paths
    - Keyword reverse index (keyword → files)
    - Stats summary

  Stage 2 (AI, one call):
    - Overall summary of the knowledge base
    - Search strategy guide by query type
    - Reads ONLY structure info (no chunk content), ~3K tokens input

Output: knowledge_map.yaml — readable by any Agent/RAG system.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ..config import get_nested

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage 1: Automatic extraction (zero cost)
# ---------------------------------------------------------------------------

def _extract_keywords_from_text(text: str, min_len: int = 2) -> list[str]:
    """
    Extract keywords from text using simple rules (no NLP library needed).

    Strategy:
      - Latin words: split by whitespace/punctuation, keep len >= min_len
      - CJK sequences: keep runs of CJK chars with len >= min_len
      - Filter out common stop words
    """
    words: list[str] = []

    # Extract latin words (English, acronyms, etc.)
    latin_words = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", text)
    words.extend(w for w in latin_words if len(w) >= min_len)

    # Extract CJK word-like sequences
    # CJK runs between punctuation/whitespace/latin chars
    cjk_runs = re.findall(r"[\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]{2,}", text)
    words.extend(cjk_runs)

    return words


# Common stop words (keep minimal — don't over-filter)
_STOP_WORDS = {
    # Japanese particles/common words
    "の", "は", "を", "が", "に", "で", "と", "も", "から", "まで",
    "する", "ある", "いる", "なる", "できる", "れる", "られる",
    "この", "その", "あの", "こと", "もの", "ため", "よう",
    "した", "して", "され", "について", "おける", "および",
    # English common words
    "the", "and", "for", "with", "from", "that", "this", "are", "was",
    "not", "but", "can", "has", "have", "will", "all", "any", "our",
    "None", "none", "True", "False", "true", "false",
    # Docling internal group names (not meaningful keywords)
    "group", "slide", "list",
}


def build_stage1(
    index_data: dict[str, Any],
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Build Stage 1 knowledge map from index.json and chunks.jsonl data.

    Returns a dict ready to be serialized to YAML.
    """
    # --- File-level info ---
    files_info: list[dict[str, Any]] = []
    all_keywords_by_file: dict[str, Counter] = defaultdict(Counter)

    for file_entry in index_data.get("files", []):
        path = file_entry.get("path", "")
        original = file_entry.get("original_file", "")

        # Collect sections from index (filter out meaningless group names)
        sections = [
            s for s in file_entry.get("sections", [])
            if s and not re.match(r"^(group|slide-\d+|list)$", s, re.IGNORECASE)
        ]

        # Collect sheet names and title_paths from chunks
        file_chunks = [c for c in chunks if c.get("metadata", {}).get("source") == path]
        sheet_names = sorted(set(
            c["metadata"]["sheet_name"]
            for c in file_chunks
            if c.get("metadata", {}).get("sheet_name")
        ))
        title_paths = sorted(set(
            c["metadata"]["title_path"]
            for c in file_chunks
            if c.get("metadata", {}).get("title_path")
        ))

        # Extract keywords from headings/title_paths (highest quality source)
        keyword_sources = sections + title_paths + sheet_names
        raw_keywords: list[str] = []
        for text in keyword_sources:
            raw_keywords.extend(_extract_keywords_from_text(text))

        # Count and filter
        kw_counter = Counter(raw_keywords)
        filtered_keywords = [
            kw for kw, _ in kw_counter.most_common(30)
            if kw not in _STOP_WORDS
        ]
        all_keywords_by_file[path] = Counter(dict(
            (kw, cnt) for kw, cnt in kw_counter.items() if kw not in _STOP_WORDS
        ))

        # Detect language: prefer index, fallback to chunks metadata
        language = file_entry.get("language", "")
        if not language and file_chunks:
            chunk_langs = [c["metadata"].get("language", "") for c in file_chunks if c.get("metadata", {}).get("language")]
            if chunk_langs:
                language = Counter(chunk_langs).most_common(1)[0][0]

        file_info: dict[str, Any] = {
            "path": path,
            "original": original,
            "format": file_entry.get("format", "unknown"),
            "language": language,
            "chunks": file_entry.get("chunks_count", len(file_chunks)),
            "tokens": file_entry.get("tokens_estimated", 0),
        }

        # Add structure info (only what's available)
        if sections:
            file_info["sections"] = sections[:20]  # Cap at 20
        if sheet_names:
            file_info["sheets"] = sheet_names
        if filtered_keywords:
            file_info["keywords"] = filtered_keywords[:15]

        files_info.append(file_info)

    # --- Keyword reverse index ---
    keyword_to_files: dict[str, list[str]] = defaultdict(list)
    for path, kw_counter in all_keywords_by_file.items():
        for kw in kw_counter:
            keyword_to_files[kw].append(path)

    # Sort by number of files (most cross-file keywords first), then alphabetically
    sorted_keywords = sorted(
        keyword_to_files.items(),
        key=lambda x: (-len(x[1]), x[0]),
    )
    # Keep top 50 most relevant keywords
    keyword_index = {kw: files for kw, files in sorted_keywords[:50]}

    # --- Languages and formats ---
    languages = sorted(set(
        f.get("language", "") for f in files_info if f.get("language")
    ))
    formats = sorted(set(f.get("format", "") for f in files_info))

    # --- Build final structure ---
    knowledge_map: dict[str, Any] = {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": {
            "total_files": len(files_info),
            "total_chunks": sum(f.get("chunks", 0) for f in files_info),
            "total_tokens": sum(f.get("tokens", 0) for f in files_info),
            "languages": languages,
            "formats": formats,
        },
        "files": files_info,
        "keyword_index": keyword_index,
    }

    return knowledge_map


# ---------------------------------------------------------------------------
# Stage 2: AI summary (one call, reads only structure)
# ---------------------------------------------------------------------------

def enrich_with_ai(
    knowledge_map: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """
    Add AI-generated summary and search guide to the knowledge map.

    Sends ONLY structure info to AI (~3K tokens), not chunk content.
    """
    from ..models.provider import text_completion

    # Build compact input for AI
    files_summary = []
    for f in knowledge_map.get("files", []):
        entry = f"{f['original']} ({f['format']}, {f.get('language','?')}): "
        if f.get("sections"):
            entry += "sections=[" + ", ".join(f["sections"][:5]) + "]"
        if f.get("sheets"):
            entry += "sheets=[" + ", ".join(f["sheets"][:5]) + "]"
        if f.get("keywords"):
            entry += " keywords=[" + ", ".join(f["keywords"][:8]) + "]"
        files_summary.append(entry)

    top_keywords = list(knowledge_map.get("keyword_index", {}).keys())[:20]

    prompt = f"""You are analyzing a knowledge base structure. Based on the following information,
generate TWO things in the SAME language as the documents (detect from filenames/keywords):

1. "summary": A 2-3 sentence overview of what this knowledge base contains
2. "search_guide": A list of 3-5 search strategy recommendations, each with:
   - query_type: type of question
   - strategy: recommended search approach
   - target_files: which files to search
   - examples: 1-2 example queries

Files in this knowledge base:
{chr(10).join(files_summary)}

Cross-file keywords: {', '.join(top_keywords)}

Stats: {knowledge_map['stats']['total_files']} files, {knowledge_map['stats']['total_chunks']} chunks, {knowledge_map['stats']['total_tokens']} tokens

Output as valid YAML (no markdown fences, no explanation, just YAML):
summary: |
  ...
search_guide:
  - query_type: ...
    strategy: ...
    target_files: [...]
    examples: [...]
"""

    model_config = get_nested(config, "models.chunking_assist", {})

    try:
        response = text_completion(
            prompt=prompt,
            model_config=model_config,
            max_tokens=2000,
        )

        # Parse AI response as YAML
        ai_data = yaml.safe_load(response)
        if isinstance(ai_data, dict):
            if "summary" in ai_data:
                knowledge_map["summary"] = ai_data["summary"]
            if "search_guide" in ai_data:
                knowledge_map["search_guide"] = ai_data["search_guide"]
        else:
            logger.warning("AI response was not valid YAML dict, skipping")

    except Exception as e:
        logger.warning(f"AI summary generation failed: {e}. Stage 1 data preserved.")

    return knowledge_map


# ---------------------------------------------------------------------------
# Write to file
# ---------------------------------------------------------------------------

def write_knowledge_map(
    knowledge_map: dict[str, Any],
    output_dir: Path,
    config: dict[str, Any],
) -> Path:
    """Write knowledge map to YAML file."""
    filename = get_nested(config, "knowledge_map.output_file", "knowledge_map.yaml")
    output_path = output_dir / filename

    output_path.write_text(
        yaml.dump(
            knowledge_map,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=120,
        ),
        encoding="utf-8",
    )

    return output_path


def write_skill_md(
    knowledge_map: dict[str, Any],
    output_dir: Path,
) -> Path:
    """
    Render knowledge map as a .SKILL.md file for Agent consumption.

    This is the same data as knowledge_map.yaml but formatted as natural
    language Markdown that an Agent can read directly as instructions.
    """
    lines: list[str] = []

    lines.append("# 知識ベース検索")
    lines.append("")

    # Summary
    summary = knowledge_map.get("summary", "")
    if summary:
        lines.append("## 概要")
        lines.append("")
        lines.append(summary.strip())
        lines.append("")

    # File listing as table
    files = knowledge_map.get("files", [])
    if files:
        lines.append("## ファイル一覧")
        lines.append("")
        lines.append("| ファイル | 形式 | 言語 | chunks | 主要セクション |")
        lines.append("|---------|------|------|--------|--------------|")
        for f in files:
            sections = f.get("sections", f.get("sheets", []))
            sec_str = ", ".join(sections[:4])
            if len(sections) > 4:
                sec_str += f" ... (+{len(sections)-4})"
            lines.append(
                f"| {f.get('original', '')} | {f.get('format', '')} "
                f"| {f.get('language', '')} | {f.get('chunks', 0)} | {sec_str} |"
            )
        lines.append("")

    # Search guide
    search_guide = knowledge_map.get("search_guide", [])
    if search_guide:
        lines.append("## 検索ガイド")
        lines.append("")
        for entry in search_guide:
            qt = entry.get("query_type", "")
            strategy = entry.get("strategy", "")
            targets = entry.get("target_files", [])
            examples = entry.get("examples", [])

            lines.append(f"### {qt}")
            lines.append(f"- **戦略**: {strategy}")
            if targets:
                lines.append(f"- **対象ファイル**: {', '.join(targets)}")
            if examples:
                lines.append(f"- **例**: {', '.join(examples)}")
            lines.append("")

    # Keyword index
    keyword_index = knowledge_map.get("keyword_index", {})
    if keyword_index:
        lines.append("## キーワード索引")
        lines.append("")
        for kw, file_paths in keyword_index.items():
            short_paths = [Path(p).stem for p in file_paths]
            lines.append(f"- **{kw}**: {', '.join(short_paths)}")
        lines.append("")

    # Stats
    stats = knowledge_map.get("stats", {})
    if stats:
        lines.append("## 統計")
        lines.append("")
        lines.append(f"- ファイル数: {stats.get('total_files', 0)}")
        lines.append(f"- チャンク数: {stats.get('total_chunks', 0)}")
        lines.append(f"- トークン数: {stats.get('total_tokens', 0):,}")
        lines.append(f"- 言語: {', '.join(stats.get('languages', []))}")
        lines.append("")

    content = "\n".join(lines)
    output_path = output_dir / "knowledge_search.SKILL.md"
    output_path.write_text(content, encoding="utf-8")

    return output_path


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_knowledge_map(
    index_path: Path,
    chunks_path: Path,
    output_dir: Path,
    config: dict[str, Any],
) -> Path | None:
    """
    Generate knowledge map from pipeline outputs.

    Args:
        index_path: Path to index.json
        chunks_path: Path to chunks.jsonl
        output_dir: Output directory
        config: Full config dict

    Returns:
        Path to written knowledge_map.yaml, or None if disabled.
    """
    if not get_nested(config, "knowledge_map.enabled", True):
        return None

    # Load index.json
    try:
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Cannot read index.json: {e}")
        return None

    # Load chunks.jsonl
    chunks: list[dict] = []
    try:
        for line in chunks_path.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                chunks.append(json.loads(line))
    except Exception:
        pass  # chunks may not exist if chunking disabled

    # Stage 1: automatic
    knowledge_map = build_stage1(index_data, chunks)

    # Stage 2: AI summary (if enabled)
    if get_nested(config, "knowledge_map.ai_summary", True):
        knowledge_map = enrich_with_ai(knowledge_map, config)

    # Write YAML
    output_path = write_knowledge_map(knowledge_map, output_dir, config)
    logger.info(f"Knowledge map written to {output_path}")

    # Write SKILL.md (Agent-readable version)
    skill_path = write_skill_md(knowledge_map, output_dir)
    logger.info(f"Skill file written to {skill_path}")

    return output_path
