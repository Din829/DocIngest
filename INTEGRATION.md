# Integrating DocIngest

How to embed DocIngest in your own system. This document is **scenario-driven and intentionally non-exhaustive** — it points you in the right direction and links to the source of truth (docstrings / `default.yaml` / `README.md`) for the details. Field-level reference belongs in those places; this is the map, not the manual.

DocIngest is a moving target. Treat the recipes here as starting points, not contracts.

---

## Pick an integration mode

| Mode | Entry point | Best for | Progress visible? | Process model |
|---|---|---|---|---|
| **CLI subprocess** | `docingest run / inspect / refine` | Shell pipelines, language-agnostic callers, one-shot batch jobs | stderr only (banner / Rich) — JSON on stdout via `--json` | Separate process |
| **Python library** | `import docingest; docingest.ingest(...)` | Embedded in another Python app — RAG pipelines, web backends, batch workers | `on_progress` callback | In-process (sync) |
| **MCP server** | `python -m docingest.mcp_server` | LLM agents (Claude Desktop / Code, Cursor, VS Code Copilot) calling DocIngest as a tool | Single response per call | Separate process, stdio/SSE transport |

Public Python API surface is exactly what `docingest/__init__.py` re-exports — `ingest`, `inspect`, `refine`, `IngestResult`, `build_config`, and the Provider classes. Everything else (`docingest.pipeline`, `docingest.parsers`, `docingest.chunkers`, ...) is internal and may change without notice.

**Optional graph layer** — `docingest.graph` is a separately-versioned subpackage exposing `build` / `query` / `status` for an opt-in GraphRAG layer on top of the main pipeline's outputs. It is NOT auto-imported and requires `pip install -e ".[graph]"`. Import explicitly: `import docingest.graph`. See README.md "GraphRAG (optional)" and ARCHITECTURE.md §10 for details.

---

## Scenarios

### 1. Backend RAG batch (no UI)

You have a folder of documents and a vector store. Process once, push chunks into the store, done.

```python
import docingest

result = docingest.ingest(
    "./docs/",
    output="./kb/",
    outputs=["chunks"],                # skip markdown / knowledge_map I/O
    vision=docingest.GeminiProvider(api_key=settings.gemini_key),
)
your_vector_db.upsert(result.chunks)
```

**Key knobs**: `outputs` whitelist (drop what you don't need — see "Output whitelist" below). `force=True` only when you've changed config that didn't auto-invalidate the cache.

### 2. Web service with live progress (FastAPI + SSE)

Show the user a real-time progress bar while documents are being processed.

```python
import asyncio, json
import docingest
from fastapi.responses import StreamingResponse

@app.post("/ingest")
async def ingest(paths: list[str]):
    queue: asyncio.Queue = asyncio.Queue()

    def on_progress(event: dict) -> None:
        queue.put_nowait(event)

    def run() -> None:
        docingest.ingest(paths, output=tmp_dir, on_progress=on_progress)
        queue.put_nowait({"kind": "done"})

    asyncio.create_task(asyncio.to_thread(run))

    async def stream():
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("kind") == "done":
                break

    return StreamingResponse(stream(), media_type="text/event-stream")
```

**Key knobs**: `on_progress` fires once per file (cached / added / updated / failed / skipped). Event schema is documented in the `run_pipeline` docstring; treat it as a forward-compatible dict (don't `KeyError` on missing fields). Run the sync pipeline in a thread pool — `ingest()` is not async.

### 3. Long-running daemon embedding

DocIngest is one capability inside a larger always-on service (worker queue, scheduler, multi-tenant API).

- **Don't let DocIngest take over signal handling.** `install_signal_handler` defaults to `False` for library callers exactly for this reason — your Ctrl+C / SIGTERM logic stays in effect.
- **Run each call in an isolated `output_dir`.** Two concurrent `ingest()` calls writing to the same directory will clobber each other's `chunks.jsonl` / `index.json` (no internal locking).
- **Inject credentials at call time.** Pass `vision=GeminiProvider(api_key=tenant_key)` rather than mutating `os.environ`; per-call providers don't leak across tenants.
- **Cache hits are nearly free.** Re-running with the same `output` reuses the incremental cache automatically — design your job queue to point at the same directory rather than rebuilding from scratch.

### 4. Agent via MCP

Claude / Cursor / Copilot agents talking to DocIngest as a tool.

```jsonc
// Claude Code: .mcp.json (project) or ~/.claude.json (personal)
{
  "mcpServers": {
    "docingest": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "docingest.mcp_server"]
    }
  }
}
```

Stdio is the default transport. For browser-side / web clients, run `python -m docingest.mcp_server --transport sse` and connect over SSE instead.

Tools exposed: `inspect`, `run`, `refine` (and the four optional `build_graph` / `query_graph` / `graph_status` / `enrich_chunks` tools when `[graph]` extras are installed). Every tool docstring spells out WHEN TO USE / TYPICAL WORKFLOW / how to interpret the result — agents read those at startup. The MCP layer is a thin transport wrapper around the same Python facade, so tool behaviour mirrors `docingest.ingest()` exactly.

Browsing / searching / reading the produced knowledge base is **deliberately NOT exposed as MCP tools** — DocIngest is a preprocessing engine, not a retrieval engine. Each agent already has Grep / Read / Glob that out-perform any wrapper we could ship, and the auto-generated `<output_dir>/knowledge_search.SKILL.md` gives them the corpus summary, file index, keyword index, and a language-routed search protocol in one Read. This keeps DocIngest as the universal upstream layer — every agent uses its own native tooling on the produced artefacts.

The recommended agent flow for any non-trivial input: **`inspect` → review cost → `run` → native Read on `knowledge_search.SKILL.md` → native Grep / Read on `sources/*.md`**. See `mcp_server.py`'s top-level `FastMCP(instructions=...)` block for the full pattern surfaced to agents at session start.

### 6. Refine for human-readable output

Refine is a separate, optional pass that uses an LLM to clean up `sources/*.md` for human consumption. It is **not** part of the RAG path — chunks come from the original sources, not from refined output.

```python
docingest.refine(["./kb/sources/spec.md"], skill="refine_faithful")
```

Two skills ship today: `refine_default` (allows light rewriting) and `refine_faithful` (preserves original wording — only dedup + format). Pick `refine_faithful` for legal / regulated content where exact wording matters.

### 5. CLI in shell pipelines

Language-agnostic callers (Node, Go, Bash) shelling out to DocIngest.

```bash
docingest inspect ./docs/ --json | jq '.[] | select(.est_cost_usd > 1.0)'
docingest run ./docs/ -o ./kb/ --json > result.json
```

Exit codes carry meaning: `0` success, `1` per-file failures occurred, `2` safety-strict abort, `130` interrupted by SIGINT. JSON goes to stdout; banner / progress / errors to stderr — pipe-friendly.

---

## Cross-cutting concerns

These apply to every integration mode.

### Configuration — three levers

Pick whichever fits your deployment, mix freely:

1. **`config_overrides=` on the call** — accepts both nested dict and flat dot-path form, mixed:
   ```python
   docingest.ingest(paths, config_overrides={
       "parsing.vision.max_pages": 200,
       "chunking": {"strategy": "heading", "max_tokens": 1024},
   })
   ```
   Best for per-call tuning from application code.

2. **`DOCINGEST__` environment variables** — double-underscore separates path segments:
   ```bash
   export DOCINGEST__chunking__max_tokens=1024
   export DOCINGEST__models__vision__primary__model=gemini-3-pro-preview
   ```
   Best for container / K8s / CI where you don't want to touch YAML.

3. **Project `docingest.yaml`** — auto-discovered in CWD, or pass `config_file=`. Best for stable per-project defaults checked into git.

Precedence (highest wins): `config_overrides` > env vars > project YAML > built-in `default.yaml`. Every config knob lives in `default.yaml` with an inline comment — that's the field-level reference.

### Credentials

Three injection paths, in order of precedence:

1. **Provider objects** — `vision=GeminiProvider(api_key="...")` on the call. Best for multi-tenant / per-call keys.
2. **Environment variables** — `GEMINI_API_KEY`, `DASHSCOPE_API_KEY`, etc. Standard 12-factor.
3. **`.env` file** — auto-loaded from CWD on import. Convenient for local dev.

Never log connection strings or full API keys. The library doesn't, and you shouldn't either when forwarding errors to users.

### Output whitelist (the biggest perf knob)

```python
docingest.ingest(paths, outputs=["chunks"])         # only chunks.jsonl
docingest.ingest(paths, outputs=["markdown"])       # only sources/*.md
docingest.ingest(paths, outputs=None)               # everything (default)
```

`outputs` actually disables the relevant pipeline stages (knowledge map generation, quality report, run log) — not just filters the read-back. On large corpora this saves real LLM cost, not just I/O. Pass only what you'll consume.

### Error classification

Each entry in `result.stats["errors"]` carries an `error_type`:

```python
for e in result.stats["errors"]:
    match e["error_type"]:
        case "timeout":     retry_later(e["file"])
        case "io_error":    notify_user(e["file"])
        case "parse_error": mark_unsupported(e["file"])
        case _:             log_unknown(e)        # forward-compatible default
```

Currently emitted: `""` (success), `timeout`, `parse_error`, `io_error`. Additional values (`chunk_error`, `interrupted`, `unknown`) are reserved for future use — match defensively with a default arm so new types don't break your code. Always branch on this field rather than grepping the `error` string.

### Graceful interrupt vs hard stop

| Caller | Default behaviour | How to change |
|---|---|---|
| CLI | Ctrl+C × 1 finishes current file then writes aggregates (exit 130). Ctrl+C × 2 hard exits | n/a |
| Python library | Ctrl+C raises `KeyboardInterrupt` immediately (host's signal handling preserved) | Pass `install_signal_handler=True` to opt in to the CLI behaviour |

Re-runs always pick up where they left off via the incremental cache — no manual checkpoint logic needed.

### Concurrency

- **In-process parallelism**: Vision API calls within a single `ingest()` are already parallelised (`performance.parallel_files`, default 4).
- **Cross-process / cross-call**: not coordinated. Each independent `ingest()` call needs its own `output_dir`. If you must share a directory, serialise calls externally.

### Cost control

Phase 0 safety check (`safety.mode`) flags files / runs over budget **before** any LLM call. Three modes:

- `off` — no checks
- `warn` (default) — log violations, proceed
- `strict` — abort unless the caller passes `acknowledge_large=True` (function parameter on `ingest()` / MCP `run` / CLI `--yes`)

Defaults are tuned for small projects. Raise thresholds in `docingest.yaml` for larger workloads. See `safety:` section in `default.yaml` for every knob.

---

## What NOT to do

- **Don't run two `ingest()` calls against the same `output_dir` concurrently.** No internal locking; second call clobbers the first's aggregates.
- **Don't import from `docingest.pipeline` / `.parsers` / `.chunkers` / `.hooks` / `.output`** in consumer code. Those are internal — use the public surface from `docingest.__init__`.
- **Don't pass `force=True` "to be safe".** The incremental cache is content-addressed and self-invalidating; forcing rebuilds is expensive on large corpora and almost never needed.
- **Don't treat `chunks.jsonl` as human-readable.** It's machine fodder for a vector store. For human consumption use `sources/*.md` (raw) or `refine` output (cleaned up).
- **Don't install the SIGINT handler in a long-running web service.** `install_signal_handler=True` is for stand-alone runs; in a worker it competes with the host's shutdown logic.
- **Don't catch `KeyboardInterrupt` and retry blindly.** A user hitting Ctrl+C in your CLI tool means they want to stop; respect it.

---

## Pointers (real source of truth)

- **Field-level config reference** → `config/default.yaml` (every knob has an inline comment)
- **API signatures and behaviour** → docstrings on `docingest.ingest`, `docingest.inspect`, `docingest.refine`, `docingest.run_pipeline`
- **Internal architecture (Phase / hooks / parsers / chunkers)** → [ARCHITECTURE.md](ARCHITECTURE.md)
- **Install / CLI / YAML examples** → [README.md](README.md)
- **Agent-side workflow advice** → [AGENTS.md](AGENTS.md)
