"""
DocIngest MCP Server — thin wrapper exposing DocIngest as MCP tools.

Architecture:
  This file is a TRANSPORT LAYER ONLY. It converts MCP tool calls into
  DocIngest Python API calls. All business logic lives in the core modules
  (pipeline.py, inspect.py, refine.py, config.py). This file should never
  contain document processing logic.

Adding a new tool:
  1. Add a @mcp.tool function below
  2. Call the corresponding DocIngest Python API
  3. Return structured result (dict/str)
  That's it. No protocol knowledge needed.

Running:
  python -m docingest.mcp_server                    # stdio (default)
  python -m docingest.mcp_server --transport sse    # SSE for web clients

Requires: pip install fastmcp   (or pip install -e ".[mcp]")
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# Lazy imports — core modules are heavy (Docling, etc.), only load when needed.
# This keeps MCP server startup fast (tool listing doesn't need Docling).

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="DocIngest",
    instructions=(
        "DocIngest is a document-preprocessing engine: any input "
        "(PDF / PPT / Excel / HTML / images / audio / video / ZIP / URLs) "
        "becomes clean Markdown + chunked text + a searchable knowledge "
        "map. Two downstreams consume the same output:\n"
        "- RAG: vector search on chunks.jsonl (your own embedder)\n"
        "- Agentic Search: grep / read on sources/*.md (via the tools below)\n"
        "\n"
        "DocIngest is NOT a retrieval engine — it does no embeddings, no "
        "vector search, no LLM answer generation. It prepares the data; "
        "the Agent (you) retrieves, cites, and reasons over it.\n"
        "\n"
        "TOOL ROLES AT A GLANCE\n"
        "- inspect(paths)              — pre-flight cost estimate, NO parsing\n"
        "- run(paths, output_dir)      — do the actual preprocessing\n"
        "- list_knowledge(dir)         — inventory a processed base\n"
        "- search_knowledge(q, dir)    — keyword grep over sources/*.md\n"
        "- read_source(name, dir)      — read one full source file\n"
        "- refine(files)               — OPTIONAL AI polish for humans\n"
        "\n"
        "WORKFLOWS YOU SHOULD REACH FOR\n"
        "\n"
        "A) User gave you new documents to ingest:\n"
        "   inspect(paths) -> review est_cost_usd and recommendation\n"
        "   run(paths, output_dir)\n"
        "   list_knowledge(output_dir) -> confirm what landed\n"
        "\n"
        "B) User asks a question about an existing knowledge base:\n"
        "   list_knowledge(dir) -> see files + sections + summary\n"
        "   search_knowledge(query, dir) -> find matching lines\n"
        "   read_source(file, dir, max_lines=200) -> read the relevant one\n"
        "\n"
        "C) User wants a cleaned / human-readable version:\n"
        "   refine(files, skill='refine_default')  (or 'refine_faithful'\n"
        "   when the user's domain is legal / contractual / compliance)\n"
        "   read_source(file, dir, folder='readable') -> fetch polished\n"
        "\n"
        "IMPORTANT HABITS\n"
        "- ALWAYS `inspect` first for unknown or large inputs. Going\n"
        "  straight to `run` can silently cost tens of dollars in Vision\n"
        "  API calls on a 300-page PDF.\n"
        "- `run` is incremental: re-running on the same output_dir is\n"
        "  cheap. Use `force=True` only when truly needed (e.g. chunking\n"
        "  strategy changed and cache didn't auto-invalidate).\n"
        "- `config_overrides` accepts BOTH flat dot-path form\n"
        "  ({'parsing.vision.max_pages': 200}) AND nested dict form\n"
        "  ({'parsing': {'vision': {'max_pages': 200}}}). Pick whichever\n"
        "  is shorter; they mix freely.\n"
        "- When `run` returns status='aborted_by_safety', DON'T retry\n"
        "  silently — surface the violation summary (cost, pages) to the\n"
        "  user, then retry with acknowledge_large=True if they agree.\n"
    ),
)


# ---------------------------------------------------------------------------
# Tool implementations — all route through the public facade (docingest.api).
#
# MCP is kept as a THIN adapter: each @mcp.tool here maps directly onto a
# function in docingest.api so config resolution / provider injection /
# output whitelisting lives in exactly ONE place. Never replicate business
# logic here — if you think you need to, add it to api.py and call that.
# ---------------------------------------------------------------------------


@mcp.tool
def inspect(
    paths: list[str],
    config_overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Pre-flight check — reports size / pages / estimated cost WITHOUT parsing.

    WHEN TO USE
    - ALWAYS call this first when the user gives you unknown / large /
      expensive inputs (PDFs over 20MB, whole directories, URLs, or any
      input whose cost profile you cannot estimate). Skipping inspect
      and going straight to `run` can silently incur 100+ Vision API
      calls on a single file.
    - SKIP ONLY when the input is clearly small and cheap: a handful of
      short text / markdown files, or content that was already processed
      in a previous turn.

    WHAT THE RESULT TELLS YOU (per-file keys)
    - `size_mb`, `pages` — raw volume. `pages` is the primary cost driver
      (1 Vision call per page by default, up to parsing.vision.max_pages).
    - `est_cost_usd` — approximate Vision spend if you `run` with current
      config. If too high: either cap via config_overrides
      (parsing.vision.max_pages), disable Vision
      (parsing.vision.enabled=false), or ask the user to confirm.
    - `recommendation` — "Ready" means all safety thresholds pass. Any
      other string lists the violations (e.g. "pages=320 > 50"); decide
      whether to proceed with `acknowledge_large=True` or tune config.
    - Format-specific fields may appear: `sheets` (xlsx), `duration_sec`
      (audio/video), `files_inside` (zip), `words` (docx).

    TYPICAL WORKFLOW
        results = inspect(["./docs/"])
        # review est_cost_usd + recommendation
        if total_cost_ok:
            run(["./docs/"])
        else:
            # ask user OR lower max_pages OR disable Vision

    Args:
        paths: File paths, directory paths, or URLs. Directories are
            expanded recursively (hidden files skipped). URLs (http / https,
            e.g. YouTube, Bilibili) resolved via yt-dlp.
        config_overrides: Override any config value per call. Accepts
            BOTH flat dot-path form ({"parsing.vision.max_pages": 200})
            AND nested dict form
            ({"parsing": {"vision": {"max_pages": 200}}}) — mix freely.
            Common overrides for inspect:
            - parsing.vision.max_pages — cap affects est_cost_usd
            - parsing.vision.enabled — set false to zero out cost estimate

    Returns:
        List of dicts, one per file. Always includes: name, path, format,
        size_mb, est_cost_usd, recommendation. Usually includes: pages,
        chars_est. Format-specific fields vary (see above).
    """
    from .api import inspect as api_inspect

    return api_inspect(paths, config_overrides=config_overrides)


# ---------------------------------------------------------------------------
# Tool: run
# ---------------------------------------------------------------------------

@mcp.tool
def run(
    paths: list[str],
    output_dir: str = "./knowledge",
    no_chunks: bool = False,
    strategy: str | None = None,
    force: bool = False,
    acknowledge_large: bool = False,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Process documents into a knowledge base (Markdown + chunks + index).

    Converts any document (PDF / PPT / Excel / HTML / images / audio /
    video / ZIP / URLs) into clean Markdown + chunked text for RAG +
    a searchable knowledge map. Incremental by default: unchanged files
    from a previous run are cached and skipped automatically.

    WHEN TO USE
    - This is the main processing tool. Call it after `inspect` confirms
      the inputs are acceptable (for large / unknown inputs) or directly
      (for small trusted inputs).
    - SAFE to re-run on the same output_dir: the incremental cache means
      only new / changed files are re-processed.

    HOW TO CHOOSE THE ARGUMENTS

    `no_chunks=True` — produce ONLY clean Markdown, skip chunks.jsonl.
        Use when the downstream consumer only needs human-readable docs,
        NOT a RAG vector store. Saves time on large corpora.

    `strategy` — override chunking strategy. Default "auto" is usually right.
        Override only when you know the document structure:
        - "heading"    : force H1-H3 splits (good for well-structured reports)
        - "recursive"  : force paragraph / sentence splits (good for prose)
        - "slide"      : PPTX slide boundaries
        - "sheet"      : XLSX sheet boundaries
        Other values rejected.

    `force=True` — ignore the incremental cache and re-process everything.
        Use sparingly: large corpora re-runs are expensive. Appropriate
        when you've changed config that affects output (e.g. chunking
        strategy) but the cache didn't invalidate automatically, or
        when debugging.

    `acknowledge_large=True` — ONLY meaningful in safety.mode="strict".
        Pre-run budget check flags oversized files → run aborts → you
        pass this True to proceed anyway AFTER reviewing the violation
        report. Recommended Agent workflow:
          1) call `inspect(paths)` → note est_cost_usd and recommendation
          2) call `run(paths)` (no acknowledge_large)
          3) if result["status"] == "aborted_by_safety": inspect
             result["safety"]["violations"], decide if cost is acceptable
          4) if OK: retry `run(paths, acknowledge_large=True)`

    HANDLING "aborted_by_safety"
    When strict mode refuses, the return dict has:
        {"status": "aborted_by_safety",
         "safety": {"mode": "strict", "violations": [...],
                    "summary": {total_files, total_pages,
                                total_est_cost_usd}}}
    Don't retry blindly — surface the cost to the user FIRST.

    Args:
        paths: File paths, directory paths, or URLs (http / https).
            Directories expanded recursively. Mixed inputs allowed.
        output_dir: Base output directory. Default "./knowledge". The
            incremental cache lives under output_dir/.cache — re-using
            the same output_dir is how you hit the cache.
        no_chunks: If True, skip chunks.jsonl. See above.
        strategy: Override chunking strategy. None keeps config default
            (auto). See above for valid values.
        force: Ignore incremental cache. See above.
        acknowledge_large: Pass True ONLY after reviewing safety violations.
            See above.
        config_overrides: Override any config value. Accepts BOTH flat
            dot-path ({"parsing.vision.max_pages": 200}) AND nested dict
            forms — mix freely. Commonly useful:
            - parsing.vision.max_pages — cap Vision calls per file
            - parsing.vision.enabled=false — skip Vision entirely
            - chunking.max_tokens — chunk size cap
            - sanitize.enabled=true — opt-in PII masking (default off)

    Returns:
        dict with processing summary:
            total_files, successful, failed, total_chunks, total_tokens,
            elapsed_ms, errors (list of {file, error}), quality (Vision
            marker scan summary).
        Optional keys:
            safety — present when Phase 0 ran with violations.
            status — "aborted_by_safety" when strict mode refused.
    """
    from .api import ingest

    # Translate MCP's positional-ish args into the facade's `outputs` and
    # `config_overrides` forms. The MCP contract stays identical; the
    # facade handles the heavy lifting.
    extra: dict[str, Any] = {}
    if strategy:
        extra["chunking.strategy"] = strategy
    outputs: list[str] | None = None
    if no_chunks:
        # Legacy flag: "Markdown only". We do not include "chunks" in the
        # whitelist so chunking is disabled AND the chunks reader is skipped.
        # Other optional outputs (index, knowledge_map, quality_report,
        # run_log) remain enabled to match the historical MCP behaviour
        # (no_chunks was narrowly about chunks.jsonl, not broad pruning).
        outputs = ["markdown", "index", "knowledge_map", "quality_report", "run_log"]

    merged_overrides: dict[str, Any] = dict(config_overrides) if config_overrides else {}
    merged_overrides.update(extra)

    result = ingest(
        paths,
        output=output_dir,
        outputs=outputs,
        config_overrides=merged_overrides or None,
        force=force,
        acknowledge_large=acknowledge_large,
    )

    stats = result.stats
    out: dict[str, Any] = {
        "total_files": stats.get("total_files", 0),
        "successful": stats.get("successful", 0),
        "failed": stats.get("failed", 0),
        "total_chunks": stats.get("total_chunks", 0),
        "total_tokens": stats.get("total_tokens", 0),
        "elapsed_ms": stats.get("elapsed_ms", 0),
        "errors": stats.get("errors", []),
        "quality": stats.get("quality", {}),
    }
    # Surface Phase 0 safety report so agents can inspect violations and
    # decide whether to retry with acknowledge_large=True. Only included
    # when Phase 0 produced a non-empty dict (keeps typical successful
    # responses compact).
    safety = stats.get("safety") or {}
    if safety:
        out["safety"] = safety
        if safety.get("aborted"):
            out["status"] = "aborted_by_safety"
    return out


# ---------------------------------------------------------------------------
# Tool: refine
# ---------------------------------------------------------------------------

@mcp.tool
def refine(
    files: list[str],
    output_dir: str | None = None,
    skill: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    AI-powered cleanup of Markdown for human readability.

    Takes Markdown files from a processed knowledge base (typically
    `sources/*.md`) and produces cleaned, polished versions under
    `readable/<skill>/*.md`. Originals are NEVER modified.

    WHEN TO USE
    - ONLY when the user explicitly wants human-readable output (e.g.
      "clean this up for publishing", "produce a readable summary").
    - Do NOT call as part of a routine RAG pipeline: raw `sources/*.md`
      are already what `search_knowledge` / `read_source` / downstream
      chunk consumers want. Refine is a SEPARATE, optional human track.
    - Do NOT refine large files blindly: the LLM call cost scales with
      input tokens. The tool auto-skips files beyond
      refine.max_input_tokens (default 8000) — check `skipped` in the
      result and respond to user with a clear reason.

    SKILL CHOICE
    - "refine_default" (default): readability-first. The LLM may rewrite
      sentences, merge redundant bullets, normalise formatting. Good for
      general polishing.
    - "refine_faithful": preserves original wording EXACTLY, only
      removes duplicates and fixes layout. Use for legal / contractual /
      compliance documents where paraphrasing is unacceptable.

    TYPICAL WORKFLOW
        # User: "clean up the contract doc for sharing"
        refine(["./kb/sources/contract.md"], skill="refine_faithful")
        # Then use read_source(..., folder="readable") to fetch result.

    Args:
        files: Markdown file paths. If a file lives in `.../sources/`,
            the output root defaults to the knowledge-base root
            (one level up). Otherwise defaults to the file's parent dir.
        output_dir: Override the output root. The final path is
            output_dir/readable/<skill_short>/<filename>. Pass None to
            use the auto-inferred default above.
        skill: "refine_default" (default) or "refine_faithful". Other
            values are looked up under skills/*.SKILL.md — custom
            skills are possible but uncommon.
        config_overrides: Per-call config overrides. Commonly useful:
            - refine.max_input_tokens — raise to refine larger files
            - refine.max_output_tokens — raise if output gets truncated
            - models.chunking_assist — swap the LLM used

    Returns:
        List of per-file results:
            source          — input path
            output          — written path (empty if skipped)
            tokens_in       — input token estimate (CJK-aware)
            tokens_out      — output token estimate (0 if skipped)
            skipped         — True if skipped (too large, LLM failed, etc.)
            warning         — reason for skip, or truncation notice
        If `warning` is non-empty even for successful refines, the output
        may be truncated (check for trailing `<!-- refine-truncated -->`).
    """
    from .api import refine as api_refine

    return api_refine(
        files,
        output=output_dir,
        skill=skill,
        config_overrides=config_overrides,
    )


# ---------------------------------------------------------------------------
# Tool: search_knowledge
# ---------------------------------------------------------------------------

@mcp.tool
def search_knowledge(
    query: str,
    knowledge_dir: str = "./knowledge",
    max_results: int = 10,
) -> dict[str, Any]:
    """
    Keyword search over a processed knowledge base (grep on sources/*.md).

    Case-insensitive literal match across every source Markdown file in
    the knowledge base. Returns matched lines plus 1 line of context
    before / after. Also surfaces the knowledge_map summary so you get
    a quick overview of what's in the base.

    WHEN TO USE
    - Call this when the user asks about CONTENT inside documents
      ("find where the contract mentions X", "documents containing Y").
    - Call `list_knowledge` FIRST when you need to know what's in the
      base — search_knowledge is for finding content, not for browsing.
    - This is KEYWORD search (literal regex-escaped substring). For
      semantic / conceptual queries, this tool is NOT enough — but
      DocIngest is a preprocessing library, not a RAG engine, so there
      is no semantic search tool here. If the user needs semantic
      matching, tell them the chunks in chunks.jsonl are meant for
      downstream vector search in their RAG stack.

    TYPICAL WORKFLOW
        # User: "Which doc talks about 解約手順?"
        r = search_knowledge("解約手順", "./knowledge/pwc")
        # r["matches"] lists files + line numbers + 1-line context
        # Then drill in:
        read_source(r["matches"][0]["file"], "./knowledge/pwc")

    INTERPRETING THE RESULT
    - `knowledge_map_summary` — populated when the base was built with
      `knowledge_map.enabled=true` (default). Read this first for a
      one-paragraph overview before diving into matches.
    - `matches` — ordered by file name. Each entry has `file`, `line`
      (1-based), `context` (up to 3 lines including the match).
    - `total_matches` — capped at max_results; if equal to max_results,
      there may be more matches not shown.

    Args:
        query: Literal search string. Case-insensitive. Regex special
            characters are escaped automatically — you cannot pass regex.
            Multi-word queries match the exact phrase, not individual words.
        knowledge_dir: Path to the knowledge base root (the directory
            that contains `sources/` and `index.json`). Default "./knowledge".
        max_results: Hard cap on returned matches. Default 10 keeps the
            response small for LLM context. Raise to 30-50 for broader
            surveys, but note total_matches == max_results usually means
            "refine your query" rather than "ingest all of these".

    Returns:
        dict with keys:
            query                  — echoed input
            matches                — list of {file, line, context}
            total_matches          — count (<= max_results)
            knowledge_map_summary  — str or None
            stats                  — dict with file / chunk / token counts
                                     (from knowledge_map.yaml, or absent)
    """
    import re

    kb_dir = Path(knowledge_dir)
    results: dict[str, Any] = {"query": query, "matches": [], "knowledge_map_summary": None}

    # Load knowledge map summary if available
    km_path = kb_dir / "knowledge_map.yaml"
    if km_path.exists():
        try:
            import yaml
            km = yaml.safe_load(km_path.read_text(encoding="utf-8"))
            results["knowledge_map_summary"] = km.get("summary")
            results["stats"] = km.get("stats")
        except Exception:
            pass

    # Grep sources/*.md for query
    sources_dir = kb_dir / "sources"
    if not sources_dir.exists():
        return results

    matches: list[dict[str, Any]] = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    for md_file in sorted(sources_dir.glob("*.md")):
        try:
            lines = md_file.read_text(encoding="utf-8").split("\n")
            for line_no, line in enumerate(lines, 1):
                if pattern.search(line):
                    # Context: line before + match + line after
                    start = max(0, line_no - 2)
                    end = min(len(lines), line_no + 1)
                    context = "\n".join(lines[start:end])
                    matches.append({
                        "file": md_file.name,
                        "line": line_no,
                        "context": context,
                    })
                    if len(matches) >= max_results:
                        break
        except Exception:
            continue
        if len(matches) >= max_results:
            break

    results["matches"] = matches
    results["total_matches"] = len(matches)
    return results


# ---------------------------------------------------------------------------
# Tool: list_knowledge
# ---------------------------------------------------------------------------

@mcp.tool
def list_knowledge(
    knowledge_dir: str = "./knowledge",
) -> dict[str, Any]:
    """
    Inventory of a processed knowledge base — returns raw index.json.

    WHEN TO USE
    - Call this FIRST when starting work with an unfamiliar knowledge
      base (e.g. the user points at a directory and says "look at these
      docs"). You'll learn what files are in it, their formats / languages
      / sizes, and their section structure, all in one call.
    - Call AFTER `run` to confirm what landed.
    - Prefer `search_knowledge` when the user already knows what they
      want (a keyword, a filename) — `list_knowledge` is for surveying,
      not for finding.

    WHAT THE RESULT CONTAINS
    - `version`, `processed_at` — schema version and when the base was built.
    - `files` — list of file entries. Each entry includes:
        path             — relative .md path under sources/
        original_file    — the input filename that produced it
        format           — "pdf" / "pptx" / "docx" / "xlsx" / ...
        title            — extracted title (usually filename stem)
        language         — auto-detected: ja / zh / en / ko / unknown
        pages            — page count (for paginated formats)
        chunks_count     — how many chunks this file produced
        tokens_estimated — CJK-aware token estimate
        sections         — section headings extracted from the doc
        element_boxes    — PDF bounding boxes per page (only for PDFs,
                           useful for citation / highlighting in RAG UIs)

    TYPICAL WORKFLOW
        info = list_knowledge("./knowledge/pwc")
        # Scan info["files"] — note format distribution, languages,
        # total token volume. Use sections to hint search queries.
        # Then: search_knowledge("...", "./knowledge/pwc")

    HANDLING ERRORS
    Returns {"error": "..."} when the knowledge base doesn't exist
    (`run` not called yet) or index.json is corrupt. Agents should tell
    the user to run `run` first when this happens.

    Args:
        knowledge_dir: Path to the knowledge base root. Default "./knowledge".

    Returns:
        dict — either full index.json content, or {"error": "..."}.
    """
    index_path = Path(knowledge_dir) / "index.json"
    if not index_path.exists():
        return {"error": f"No index.json found at {index_path}. Run 'run' first."}

    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"Failed to read index.json: {e}"}


# ---------------------------------------------------------------------------
# Tool: read_source
# ---------------------------------------------------------------------------

@mcp.tool
def read_source(
    file_name: str,
    knowledge_dir: str = "./knowledge",
    folder: str = "sources",
    max_lines: int | None = None,
) -> dict[str, Any]:
    """
    Read a full Markdown file from a processed knowledge base.

    WHEN TO USE
    - After `search_knowledge` or `list_knowledge` gives you a file name
      and you need the full content to answer the user.
    - For LARGE files, ALWAYS pass `max_lines` first (e.g. max_lines=200)
      to peek at the beginning without blowing your context window.
      Follow up with targeted reads if needed.
    - Do NOT use this to browse — call `list_knowledge` when you just
      want to know what files exist.

    FOLDER CHOICE
    - `folder="sources"` (default) — raw parsed Markdown. This is the
      ground truth used by search_knowledge and by downstream RAG.
    - `folder="readable"` — AI-refined versions produced by the `refine`
      tool. Use when the user wants the polished / human-friendly
      version. Only exists if `refine` was called previously.
      Files live under `readable/<skill_short>/` (e.g. readable/default/,
      readable/faithful/). Pass `folder="readable/default"` to target a
      specific skill.

    TRUNCATION BEHAVIOUR
    When max_lines is set and the file is longer, content is truncated
    from the top (first N lines kept). The `truncated` flag is True and
    `total_lines` reports the full file size so you can decide whether
    to read more or change strategy (e.g. ask user what section they want).

    TYPICAL WORKFLOW
        # User asked about the contract's cancellation terms:
        hits = search_knowledge("解約", "./kb")
        # hits["matches"][0]["file"] = "contract.md", line = 42
        # Peek at the file around that region:
        r = read_source("contract.md", "./kb", max_lines=60)
        # r["content"] has the first 60 lines; decide next action.

    Args:
        file_name: Filename (e.g. "report.md"). NOT a full path — just
            the name as returned by list_knowledge / search_knowledge.
        knowledge_dir: Path to the knowledge base root. Default "./knowledge".
        folder: Which subfolder to read from. "sources" (default) or
            "readable" or "readable/<skill>". See above.
        max_lines: Read only the first N lines. None = full file
            (dangerous for huge files). Recommended: pass 200 for a
            peek; increase as needed.

    Returns:
        dict with keys:
            file         — echoed file name
            folder       — echoed folder
            content      — Markdown text (possibly truncated)
            total_lines  — full line count of the file
            truncated    — bool, True if max_lines cut it off
        OR {"error": "..."} on file not found / read failure.
    """
    source_path = Path(knowledge_dir) / folder / file_name
    if not source_path.exists():
        return {"error": f"File not found: {source_path}"}

    try:
        text = source_path.read_text(encoding="utf-8")
        lines = text.split("\n")
        total_lines = len(lines)
        truncated = False

        if max_lines is not None and total_lines > max_lines:
            text = "\n".join(lines[:max_lines])
            truncated = True

        return {
            "file": file_name,
            "folder": folder,
            "content": text,
            "total_lines": total_lines,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": f"Failed to read {file_name}: {e}"}


# ---------------------------------------------------------------------------
# Optional GraphRAG tools — registered only when docingest.graph imports
# cleanly (i.e. the [graph] extras are installed). When the extras are not
# installed the tools simply don't appear in the listing; the rest of the
# MCP server is unaffected. Stays consistent with the CLI's conditional
# registration at src/docingest/cli.py.
# ---------------------------------------------------------------------------

try:
    # Probe-import only — the actual graph module is re-imported inside each
    # tool body via `from . import graph` so static type checkers can see
    # the binding regardless of whether the probe succeeded.
    from . import graph as _graph_probe  # noqa: F401 — import probe only
    _GRAPH_AVAILABLE = True
except ImportError:
    _GRAPH_AVAILABLE = False


# ---------------------------------------------------------------------------
# nest_asyncio: required so the long-running MCP server can call
# docingest.graph.query() more than once in the same process.
#
# Why this lives ONLY in the MCP entry point (not in docingest.graph
# itself):
#   - nest_asyncio.apply() is a global monkeypatch on Python's asyncio.
#     It must not be silently applied to library callers' processes —
#     embedding DocIngest into a long-running host (web service, daemon)
#     should leave the host's asyncio untouched.
#   - The MCP server, by definition, IS the long-running host for graph
#     tool calls. Agents reach it via stdio/SSE and invoke query_graph
#     repeatedly; without nest_asyncio the 2nd+ call hits LightRAG's
#     known asyncio.Lock-bound-to-first-event-loop bug and returns an
#     empty answer (now surfaced as stats["error"], but still failed).
#   - The CLI path is unaffected by this bug because each `docingest
#     graph query` invocation is its own subprocess — Locks die with
#     the process, so no apply needed there either.
#
# Failure-tolerant: if nest_asyncio isn't installed (older [graph]
# extras, or someone hand-picked deps), we silently skip — the empty-
# answer detection in lightrag_backend still makes the 2nd-call failure
# visible to the agent via stats["error"]. So worst case: degraded
# behaviour with a clear error message, never a hard crash.
# ---------------------------------------------------------------------------

if _GRAPH_AVAILABLE:
    try:
        import nest_asyncio  # type: ignore[import-not-found]
        nest_asyncio.apply()
        _NEST_ASYNCIO_APPLIED = True
    except ImportError:
        _NEST_ASYNCIO_APPLIED = False
        logger.warning(
            "nest_asyncio is not installed — repeated query_graph calls "
            "in this MCP server will fail on the 2nd attempt (LightRAG "
            "asyncio.Lock bug). Install with: pip install -e \".[graph]\""
        )

if _GRAPH_AVAILABLE:

    @mcp.tool
    def build_graph(
        knowledge_dir: str,
        mode: str | None = None,
        force: bool = False,
        config_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Build (or incrementally extend) a knowledge graph on top of an
        existing knowledge base produced by the `run` tool.

        WHEN TO USE
        - Only after a successful `run` produced chunks.jsonl in the same
          knowledge_dir. Graph build never re-parses original documents.
        - When the user wants to ask MULTI-HOP or GLOBAL questions
          ("themes / trends / connections across the corpus") that
          ordinary RAG can't answer well.
        - SKIP when the user only needs single-document or single-fact
          retrieval — `search_knowledge` is cheaper and faster.

        ARGS
        - knowledge_dir: same path you passed as output_dir to `run`.
        - mode: 'vector_only' (cheap, single/two-hop only) or 'full'
                (adds community detection + global queries). None = use
                config default ('full').
        - force: ignore the graph extraction cache and rebuild from
                scratch. Use sparingly — extraction is the most expensive
                step in the whole stack.
        - config_overrides: same nested-dict / dot-path semantics as `run`.

        IMPORTANT HABITS
        - The first build of a 1000-chunk corpus typically costs single-
          digit dollars in LLM calls. Surface the cost estimate to the
          user before running on unfamiliar data.
        - Subsequent builds are nearly free thanks to incremental cache —
          only chunks whose content changed are re-extracted.

        RETURNS
        Dict with keys: backend, mode, entities_count, relations_count,
        communities_count, chunks_processed, chunks_skipped_cached,
        elapsed_ms, output_dir, errors.
        """
        try:
            from . import graph as _graph_module
            result = _graph_module.build(
                knowledge_dir,
                mode=mode,
                force=force,
                config_overrides=config_overrides,
            )
        except (ImportError, ValueError, FileNotFoundError) as e:
            return {"error": str(e)}

        return {
            "backend": result.backend,
            "mode": result.mode,
            "entities_count": result.entities_count,
            "relations_count": result.relations_count,
            "communities_count": result.communities_count,
            "chunks_processed": result.chunks_processed,
            "chunks_skipped_cached": result.chunks_skipped_cached,
            "elapsed_ms": result.elapsed_ms,
            "output_dir": result.output_dir,
            "errors": result.errors,
        }

    @mcp.tool
    def query_graph(
        question: str,
        knowledge_dir: str,
        mode: str = "hybrid",
        top_k: int | None = None,
        config_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Query a previously-built knowledge graph.

        WHEN TO USE
        - The user asks a question that benefits from graph traversal:
          multi-hop reasoning, "what are the main themes", "how is X
          connected to Y across documents". Reach for this AFTER the
          knowledge_dir has been graph-built (`build_graph`).
        - SKIP for single-document fact lookup — `read_source` /
          `search_knowledge` are simpler and don't risk LLM hallucination
          atop the graph.

        ARGS
        - question: natural-language query.
        - knowledge_dir: same path you used for `build_graph`.
        - mode: 'naive' (pure vector RAG), 'local' (entity-anchored),
                'global' (community summaries — broad questions),
                'hybrid' (local + global, default), 'mix' (everything).
                A graph built with mode='vector_only' rejects 'global' /
                'hybrid' / 'mix' — call `graph_status` first to check.
        - top_k: backend-specific retrieval cutoff. None = backend default.
        - config_overrides: same as `run`.

        RETURNS
        Dict with keys: answer (the LLM's response), mode_used,
        elapsed_ms, output_dir. On configuration / file errors returns
        {"error": "..."} instead.
        """
        try:
            from . import graph as _graph_module
            result = _graph_module.query(
                question,
                knowledge_dir=knowledge_dir,
                mode=mode,
                top_k=top_k,
                config_overrides=config_overrides,
            )
        except (ImportError, ValueError, FileNotFoundError) as e:
            return {"error": str(e)}

        return {
            "answer": result.answer,
            "mode_used": result.mode_used,
            "elapsed_ms": result.elapsed_ms,
            "output_dir": result.output_dir,
        }

    @mcp.tool
    def graph_status(knowledge_dir: str) -> dict[str, Any]:
        """
        Inspect whether a knowledge base has a graph built.

        WHEN TO USE
        - Before calling `query_graph`, especially when you're not sure
          whether `build_graph` has been run on this knowledge_dir.
        - To present the user with current graph stats (entity / relation
          / community counts) before deciding whether a rebuild is needed.

        ARGS
        - knowledge_dir: knowledge base root.

        RETURNS
        Dict with keys: built (bool), backend, mode, entities_count,
        relations_count, communities_count, last_built_at,
        embedding_model, embedding_dimension, output_dir.
        """
        from . import graph as _graph_module
        info = _graph_module.status(knowledge_dir)
        return {
            "built": info.built,
            "backend": info.backend,
            "mode": info.mode,
            "entities_count": info.entities_count,
            "relations_count": info.relations_count,
            "communities_count": info.communities_count,
            "last_built_at": info.last_built_at,
            "embedding_model": info.embedding_model,
            "embedding_dimension": info.embedding_dimension,
            "output_dir": info.output_dir,
        }

    @mcp.tool
    def enrich_chunks(
        knowledge_dir: str,
        config_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate chunks_enriched.jsonl from an already-built graph,
        feeding entity descriptions back into a sibling chunks file so
        traditional vector RAG also benefits from the graph's extraction.

        WHEN TO USE
        - The user wants their EXISTING vector RAG pipeline to recall
          documents better — without rewriting the RAG side.
        - The graph was built earlier (status -> built=True) but the user
          didn't pass --enrich-chunks at build time.
        - SKIP when the graph isn't built yet (call build_graph first).

        EFFECT
        - Writes a NEW file `chunks_enriched.jsonl` next to chunks.jsonl.
        - The original chunks.jsonl is NEVER modified — delete the
          enriched file at any time to drop back to the original
          behaviour.
        - No LLM / embedding calls are made — pure replay over the graph
          artefacts already on disk. Cheap.

        ARGS
        - knowledge_dir: knowledge base root (the same path you used for
          build_graph).
        - config_overrides: optional, same dict-form semantics as `run`.
          Useful knobs:
            graph.enrich_chunks.max_entities_per_chunk
            graph.enrich_chunks.max_description_length
            graph.enrich_chunks.inject_into_text
            graph.enrich_chunks.inject_into_metadata

        RETURNS
        Dict with keys: written_path, chunks_total, chunks_enriched,
        chunks_unchanged, total_entities_injected, avg_entities_per_chunk,
        elapsed_ms, errors. On configuration / file errors returns
        {"error": "..."} instead.
        """
        try:
            from . import graph as _graph_module
            result = _graph_module.enrich_chunks(
                knowledge_dir,
                config_overrides=config_overrides,
            )
        except (ImportError, ValueError, FileNotFoundError) as e:
            return {"error": str(e)}

        return {
            "written_path": result.written_path,
            "chunks_total": result.chunks_total,
            "chunks_enriched": result.chunks_enriched,
            "chunks_unchanged": result.chunks_unchanged,
            "total_entities_injected": result.total_entities_injected,
            "avg_entities_per_chunk": result.avg_entities_per_chunk,
            "elapsed_ms": result.elapsed_ms,
            "errors": result.errors,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server."""
    import sys

    transport = "stdio"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            transport = sys.argv[idx + 1]

    # Load .env for API keys
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
