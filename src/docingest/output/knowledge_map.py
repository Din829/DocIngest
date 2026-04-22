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
from .keyword_extractor import create_keyword_extractor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search-protocol templates (inserted into SKILL.md)
# ---------------------------------------------------------------------------
# Short, tool-agnostic guidance to stabilise Agent behaviour when it consumes
# this knowledge base. Intentionally prescriptive about the two-phase flow
# (RAG for coordinates → Agentic for precision) and the "事実 ≠ 結論" rule,
# because both recover accuracy that we've seen Agents lose when left to
# their own search heuristics. Kept to ~6 lines per language so it costs
# almost nothing in the Agent's context but still delivers the key rules.
# Disable via knowledge_map.search_protocol = false.

_SEARCH_PROTOCOL_TEMPLATES: dict[str, str] = {
    "ja": (
        "## 検索方針\n\n"
        "**Phase 1 (RAG 利用時)**: `chunks.jsonl` で横断検索し、候補ファイル / "
        "`title_path` / 位置を取得する。キーワードは同義語も必ず含める"
        "（例: 耐震等級 → 耐震性能 / 住宅性能 / 性能評価）。\n\n"
        "**Phase 2 (Agentic Search)**: Phase 1 の結果、または RAG が無い場合は直接、"
        "`sources/*.md` を Grep → Read で精査する。独立した列挙項目なら Grep + "
        "前後行で十分、条項・金額・日付など正確性が要る内容は Read で段落全体を確認する。\n\n"
        "**Grep 無ヒット ≠ 記載なし** — 同義語を網羅したか自己確認してから結論する。\n"
    ),
    "zh": (
        "## 检索策略\n\n"
        "**Phase 1（有 RAG 时）**：用 `chunks.jsonl` 做横断检索，"
        "拿到候选文件 / `title_path` / 位置信息。关键词必须包含同义词"
        "（例：耐震等级 → 耐震性能 / 住宅性能 / 性能評價）。\n\n"
        "**Phase 2（Agentic Search）**：以 Phase 1 的结果为起点，"
        "无 RAG 时直接开始，用 Grep → Read 精查 `sources/*.md`。"
        "独立列举项用 Grep + 上下文行足够；条款、金额、日期等需要精度的内容，"
        "用 Read 读完整段落。\n\n"
        "**Grep 零命中 ≠ 未记载** — 确认已穷举同义词后再下结论。\n"
    ),
    "en": (
        "## Search Protocol\n\n"
        "**Phase 1 (with RAG)**: Query `chunks.jsonl` for candidate files, "
        "`title_path`, and position. Always expand the query with synonyms "
        "(e.g. \"seismic grade\" → seismic rating / housing performance / "
        "performance evaluation).\n\n"
        "**Phase 2 (Agentic Search)**: Starting from Phase 1 results (or "
        "directly, if no RAG), search `sources/*.md` with Grep → Read. "
        "Grep with surrounding context is enough for independent list items; "
        "use Read to see the full paragraph for clauses, amounts, dates, or "
        "anything precision-critical.\n\n"
        "**Grep miss ≠ not written** — confirm you have exhausted synonyms "
        "before concluding.\n"
    ),
}


def _pick_protocol_template(stats: dict[str, Any]) -> str:
    """
    Pick a protocol template by the knowledge base's dominant language.
    Falls back to English for any language we don't have a template for.
    """
    langs = stats.get("languages") or []
    if langs:
        first = str(langs[0]).lower()
        if first in _SEARCH_PROTOCOL_TEMPLATES:
            return _SEARCH_PROTOCOL_TEMPLATES[first]
    return _SEARCH_PROTOCOL_TEMPLATES["en"]


# ---------------------------------------------------------------------------
# Stage 1: Automatic extraction (zero cost)
# ---------------------------------------------------------------------------

def build_stage1(
    index_data: dict[str, Any],
    chunks: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """
    Build Stage 1 knowledge map from index.json and chunks.jsonl data.

    Returns a dict ready to be serialized to YAML.
    """
    kw_cfg = get_nested(config, "knowledge_map.keywords", {})
    extractor = create_keyword_extractor(config)
    max_per_file: int = int(kw_cfg.get("max_per_file", 15))
    max_index: int = int(kw_cfg.get("max_index", 50))

    # Docling internal group names that carry no semantic meaning
    _noise_re = re.compile(r"^(group|slide-\d+|list)$", re.IGNORECASE)

    # Pre-index chunks by source path. Before this the per-file loop did
    # `[c for c in chunks if c.metadata.source == path]` inside the loop,
    # giving an O(F × C) scan (100 files × 10k chunks = 1M comparisons).
    # Building the index once up front reduces that to O(F + C). Chunks
    # whose source does not appear in index_data["files"] remain absent
    # from the output — same behaviour as the previous scan.
    chunks_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in chunks:
        src = c.get("metadata", {}).get("source") or ""
        if src:
            chunks_by_source[src].append(c)

    # --- File-level info ---
    files_info: list[dict[str, Any]] = []
    all_keywords_by_file: dict[str, Counter] = defaultdict(Counter)

    for file_entry in index_data.get("files", []):
        path = file_entry.get("path", "")
        original = file_entry.get("original_file", "")

        # Collect sections from index (filter out meaningless group names)
        sections = [
            s for s in file_entry.get("sections", [])
            if s and not _noise_re.match(s)
        ]

        # Collect sheet names and title_paths from chunks
        file_chunks = chunks_by_source.get(path, [])
        sheet_names = sorted(set(
            c["metadata"]["sheet_name"]
            for c in file_chunks
            if c.get("metadata", {}).get("sheet_name")
        ))
        title_paths = sorted(set(
            tp for c in file_chunks
            if (tp := c.get("metadata", {}).get("title_path"))
            and not _noise_re.match(tp)
        ))

        # Extract keywords from headings/title_paths (highest quality source)
        keyword_sources = sections + title_paths + sheet_names
        raw_keywords: list[str] = []
        for text in keyword_sources:
            raw_keywords.extend(extractor.extract(text))

        # Count and deduplicate
        kw_counter = Counter(raw_keywords)
        filtered_keywords = [kw for kw, _ in kw_counter.most_common(max_per_file)]
        all_keywords_by_file[path] = kw_counter

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
            file_info["keywords"] = filtered_keywords

        files_info.append(file_info)

    # --- Keyword reverse index ---
    keyword_to_files: dict[str, list[str]] = defaultdict(list)
    for path, kw_counter in all_keywords_by_file.items():
        for kw in kw_counter:
            keyword_to_files[kw].append(path)

    # Discrimination filter (TF-IDF document-frequency cut-off): words that
    # appear in too many files carry no signal for cross-file search. A
    # keyword appearing in >70% of the corpus behaves like a stop word
    # regardless of what it means — "場合" / "事項" / "ruling" in legal docs,
    # "introduction" / "conclusion" in papers. The threshold is fully
    # language-agnostic because it relies on document distribution, not
    # word lists. Tune via knowledge_map.keywords.max_doc_frequency_ratio;
    # skipped when the corpus is too small (< 3 files) to be statistical.
    total_file_count = len(files_info)
    max_df_ratio = float(kw_cfg.get("max_doc_frequency_ratio", 0.7))
    if total_file_count >= 3 and 0.0 < max_df_ratio < 1.0:
        max_files = max(1, int(total_file_count * max_df_ratio))
        keyword_to_files = {
            kw: files for kw, files in keyword_to_files.items()
            if len(files) <= max_files
        }

    # Sort by number of files (most cross-file keywords first), then alphabetically
    sorted_keywords = sorted(
        keyword_to_files.items(),
        key=lambda x: (-len(x[1]), x[0]),
    )
    keyword_index = {kw: files for kw, files in sorted_keywords[:max_index]}

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

# Required fields for every search_guide entry. A missing field is almost
# always a symptom of LLM truncation — dropping the incomplete entry is
# safer than writing a half-filled record downstream tools will parse wrong.
_SEARCH_GUIDE_REQUIRED = frozenset({"query_type", "strategy", "target_files", "examples"})


def _validate_search_guide(raw: Any) -> tuple[list[dict[str, Any]], int]:
    """
    Filter a search_guide list to well-formed entries only.

    Returns (valid_entries, dropped_count). An entry is kept only when it
    is a dict containing every field in _SEARCH_GUIDE_REQUIRED with a
    non-empty value. This protects downstream readers from the
    truncation-artifact case where the last entry of an LLM-generated list
    ends mid-field.
    """
    if not isinstance(raw, list):
        return [], 0
    valid: list[dict[str, Any]] = []
    dropped = 0
    for entry in raw:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        if not _SEARCH_GUIDE_REQUIRED.issubset(entry.keys()):
            dropped += 1
            continue
        if any(entry[k] in (None, "", [], {}) for k in _SEARCH_GUIDE_REQUIRED):
            dropped += 1
            continue
        valid.append(entry)
    return valid, dropped


def enrich_with_ai(
    knowledge_map: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """
    Add AI-generated summary and search guide to the knowledge map.

    Sends ONLY structure info to AI (~3K tokens), not chunk content.

    Three layers of protection against LLM output issues:
      L1 (config) — token budget inherited from models.defaults, no hardcode.
      L2 (retry)  — when finish_reason is "length", retry once with a larger
                    budget (models.defaults.retry_max_tokens) if the config
                    opts in via retry_on_truncation.
      L3 (schema) — drop search_guide entries missing required fields, so a
                    truncated tail never pollutes downstream consumers.
    """
    from ..models.provider import text_completion

    # Build compact input for AI. Limits chosen empirically: sections and
    # keywords give the LLM enough signal to ground its suggestions in
    # real document structure (rather than inventing plausible-but-absent
    # content), while still fitting well under the chunking_assist budget.
    #
    # Sections window is adaptive: take at least 8 headings, but for files
    # with many headings allow up to half of them (capped at 20). This means
    # small files don't waste tokens while large files — which otherwise
    # end up under-represented if we chop at 8 — get proportionally more
    # evidence. The cap prevents a single huge file from dominating the
    # prompt; 20 is also the per-file sections cap used when this
    # knowledge_map was built (see file_info["sections"] earlier).
    def _sections_window(items: list[str]) -> int:
        return min(20, max(8, len(items) // 2))

    files_summary = []
    for f in knowledge_map.get("files", []):
        entry = f"{f['original']} ({f['format']}, {f.get('language','?')}): "
        if f.get("sections"):
            entry += "sections=[" + ", ".join(f["sections"][:_sections_window(f["sections"])]) + "]"
        if f.get("sheets"):
            entry += "sheets=[" + ", ".join(f["sheets"][:_sections_window(f["sheets"])]) + "]"
        if f.get("keywords"):
            entry += " keywords=[" + ", ".join(f["keywords"][:12]) + "]"
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

Grounding requirement (important):
- Ground every query_type, strategy, and example in the sections /
  sheets / keywords listed below. Do NOT invent topics that do not
  appear in any section or keyword (e.g. don't add a query about
  "pet rules" if no file mentions pets).
- If you're uncertain whether a topic is covered, omit it rather
  than guessing. An incomplete but accurate guide is more useful
  than an exhaustive but fabricated one.

Coverage suggestion (soft):
- When the sections / keywords reasonably support it, try to ensure
  every file is referenced in at least one search_guide entry. A file
  that never appears anywhere is effectively invisible to users. But
  prefer omission over forcing: do NOT create a fake entry just to
  include a file if its content doesn't fit any genuine query_type.

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
        # L1 (budget) + L2 (retry on truncation) are handled inside
        # text_completion — it reads models.defaults.retry_on_truncation from
        # the _defaults subdict injected by load_config. We only need to
        # surface the post-retry state here for L3 schema filtering.
        response, finish_reason = text_completion(
            prompt=prompt,
            model_config=model_config,
            # max_tokens=None → resolve_max_tokens() uses models.chunking_assist
            # or models.defaults.max_response_tokens from config.
        )

        if finish_reason == "length":
            logger.warning(
                "AI summary still truncated after retry. "
                "Incomplete search_guide entries will be dropped."
            )

        # Parse AI response as YAML — tolerate truncated tail by
        # catching YAMLError and falling back to Stage 1 data.
        try:
            ai_data = yaml.safe_load(response)
        except yaml.YAMLError as e:
            logger.warning(
                f"AI response was not parseable YAML ({e}). "
                "Stage 1 data preserved."
            )
            return knowledge_map

        if not isinstance(ai_data, dict):
            logger.warning("AI response was not a YAML dict, skipping.")
            return knowledge_map

        if "summary" in ai_data and ai_data["summary"]:
            knowledge_map["summary"] = ai_data["summary"]

        # L3: schema-filter search_guide so incomplete entries never reach
        # the output file.
        if "search_guide" in ai_data:
            valid_guide, dropped = _validate_search_guide(ai_data["search_guide"])
            if dropped:
                logger.warning(
                    f"Dropped {dropped} incomplete search_guide entr"
                    f"{'y' if dropped == 1 else 'ies'} (missing required fields)."
                )
            if valid_guide:
                knowledge_map["search_guide"] = valid_guide

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
    config: dict[str, Any] | None = None,
) -> Path:
    """
    Render knowledge map as a .SKILL.md file for Agent consumption.

    This is the same data as knowledge_map.yaml but formatted as natural
    language Markdown that an Agent can read directly as instructions.

    When enabled (knowledge_map.search_protocol = true, default), a short
    two-phase search-protocol block is inserted between the Summary and
    File-listing sections. The block is hard-coded (RAG → Agentic Search
    flow with synonym expansion and "Grep miss ≠ not written" guard) — not
    user-tunable text, just an on/off switch in config. If a project needs
    different wording they can disable the block and add their own.
    """
    cfg = config or {}

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

    # Search-protocol block (language-routed)
    if get_nested(cfg, "knowledge_map.search_protocol", True):
        protocol = _pick_protocol_template(knowledge_map.get("stats", {}))
        lines.append(protocol.rstrip())
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
    knowledge_map = build_stage1(index_data, chunks, config)

    # Stage 2: AI summary (if enabled)
    if get_nested(config, "knowledge_map.ai_summary", True):
        knowledge_map = enrich_with_ai(knowledge_map, config)

    # Write YAML
    output_path = write_knowledge_map(knowledge_map, output_dir, config)
    logger.info(f"Knowledge map written to {output_path}")

    # Write SKILL.md (Agent-readable version)
    skill_path = write_skill_md(knowledge_map, output_dir, config)
    logger.info(f"Skill file written to {skill_path}")

    return output_path
