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

**Failure handling — don't fall into the "succeeded, 0 files" trap**: `ingest()` returns rather than raising, so a run where every file failed still comes back as a normal `IngestResult` with `stats["failed"] > 0`. An unaware caller that only checks "did it return" treats that as success and ships an empty knowledge base. Two safeguards:
- Failures are **always logged at warning level** (`DocIngest: N file(s) failed — ...`), even on the library path — so they surface in your logs without you inspecting `stats`.
- Pass **`raise_on_failure=True`** to turn any file failure (parse error, timeout, …) into a `RuntimeError` instead of a silent return — the fail-loud option for pipelines that must not proceed on partial results.
- Either way, the per-file detail lives in `result.stats["errors"]` (each entry: `file` / `error` / `error_type`, where `error_type` ∈ `timeout` / `parse_error` / `io_error` / `encrypted` / …).

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

**Other clients:**

```jsonc
// Claude Desktop: claude_desktop_config.json (Settings > Developer > Edit Config),
// then fully restart Claude (quit, not just close the window).
{ "mcpServers": { "docingest": {
    "command": "python", "args": ["-m", "docingest.mcp_server"], "cwd": "/path/to/DocIngest" } } }

// VS Code (Copilot): .vscode/mcp.json — note the key is `servers`, NOT `mcpServers`.
// Tools appear in Copilot Agent mode (Ctrl+Alt+I).
{ "servers": { "docingest": {
    "type": "stdio", "command": "python", "args": ["-m", "docingest.mcp_server"] } } }
```

Tools exposed: `inspect`, `run`, `refine`. Plus optional graph tools (`build_graph`, `query_graph`, `graph_status`, `enrich_chunks`) when `pip install -e ".[graph]"` extras are installed — without them the four graph tools simply don't appear in the listing. Every tool docstring spells out WHEN TO USE / TYPICAL WORKFLOW / how to interpret the result — agents read those at startup. The MCP layer is a thin transport wrapper around the same Python facade, so tool behaviour mirrors `docingest.ingest()` exactly.

**Per-call `config_overrides`** — every tool accepts a `config_overrides` dict to change behaviour without touching files. Nested or flat dot-path form, mix freely:

```python
run(["docs/"], config_overrides={"parsing": {"vision": {"max_pages": 200}}, "chunking": {"max_tokens": 1024}})
run(["docs/"], config_overrides={"parsing.vision.max_pages": 200, "chunking.max_tokens": 1024})
```

**Troubleshooting:** `Module not found` → `pip install -e ".[mcp]"`. API-key errors → set `GEMINI_API_KEY` / `DASHSCOPE_API_KEY` in `.env`, or inject via Provider classes. Large files hang → `inspect` first, then raise `max_pages` via `config_overrides`.

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

`outputs=` controls which downstream **stages** run, and which artefacts
are read back into `IngestResult`. Two artefacts are always produced —
they feed the incremental cache and other stages that may still be
enabled — the rest can be disabled when not needed.

```python
docingest.ingest(paths, outputs=["chunks"])    # skips knowledge_map / quality_report / run_log
docingest.ingest(paths, outputs=["markdown"])  # skips chunks / knowledge_map / quality_report / run_log
docingest.ingest(paths, outputs=None)          # everything (default)
```

| Output | Always written? | What `outputs=` controls when omitted |
|---|---|---|
| `sources/*.md` | **Yes** | Reader skipped — but file is still on disk (incremental cache + downstream stages need it) |
| `index.json` | **Yes** | Reader skipped — file is still on disk (small; required by cache) |
| `chunks.jsonl` | No | `chunking.enabled=false` → chunker not instantiated, chunks.jsonl not written |
| `knowledge_map.yaml` | No | `knowledge_map.enabled=false` → Phase 4 skipped, **no LLM call** for the AI summary |
| `quality_report.json` | No | `quality_report.enabled=false` → marker scan skipped |
| `log.md` | No | `run_log.enabled=false` → timeline append skipped |

So `outputs=["chunks"]` does **not** mean "only `chunks.jsonl` lands on
disk" — `sources/*.md` and `index.json` still get written. What it does
mean: knowledge_map / quality_report / run_log stages don't run, which
saves the LLM call (knowledge_map AI summary) and a bit of I/O.

On large corpora the real saver is dropping `knowledge_map` (one LLM call
per run) and `chunks` (chunker CPU time per file). Pass `outputs=` with
exactly what your consumer reads.

If you genuinely cannot afford `sources/*.md` on disk (e.g. an ephemeral
tenant directory), run `ingest()` into a `tempfile.mkdtemp()` and
`shutil.rmtree()` after consuming `result.chunks` / `result.markdown_files`
in memory. The cache then lives and dies with that temp dir.

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

`io_error` entries coming from `discover_files` (missing path, failed URL, URL disabled — see [Cross-container handoff](#cross-container--cross-process-file-handoff)) additionally carry a stable `reason` token (`"not_found"` / `"url_failed"` / `"url_disabled"`). `io_error` entries thrown by the parser itself (e.g. file disappeared mid-run) don't have a `reason` — branch on its presence with `e.get("reason")` rather than `e["reason"]`.

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
- `warn` — log violations, proceed
- `strict` (default) — abort unless the caller passes `acknowledge_large=True` (function parameter on `ingest()` / MCP `run` / CLI `--yes`)

Defaults are tuned for real-world business documents (multi-hundred-page reports, multi-MB workbooks). Raise thresholds in `docingest.yaml` for larger workloads, or drop to `warn` mode if you'd rather absorb the cost without confirmation. See `safety:` section in `default.yaml` for every knob.

---

## Deployment

DocIngest is process-local by default. Anything beyond that — containers,
cloud LLMs, persistent caches, non-root users — is the integrator's job.
This section captures the patterns that work and the traps that don't,
without prescribing one Dockerfile to rule them all.

### Image size — torch is the only thing that matters

DocIngest depends on the standard `docling` meta-package, which **pulls `torch`
transitively** (its `DocumentConverter` imports torch at module load — this is
NOT confined to a `[models-local]` extra). On Linux the default PyPI wheel is
the ~5.6GB **CUDA** build, of which DocIngest uses none (CPU inference only).
So the whole image-size question reduces to *which torch wheel you let in*:

| How you install | torch wheel | Approx. size |
|---|---|---|
| **CPU torch first, then DocIngest** (the scripts / Dockerfile.example do this) | CPU-only | ~200 MB torch |
| Plain `pip install -e .` on **Linux** (no CPU-torch step) | CUDA (default PyPI) | ~5–6 GB — wasted, DocIngest never uses the GPU |
| Plain `pip install -e .` on **Windows / macOS** | CPU (PyPI default there) | ~200 MB torch |

The lesson: **always install the CPU torch wheel before DocIngest** (or use
`scripts/install_python_deps.{sh,ps1}` / `Dockerfile.example`, which do it for
you). A Linux box that skips that step silently grows the image by ~5GB of
unused GPU libraries. Run `python scripts/verify_deps.py` after install — it
fails the build and prints the one-line fix if a CUDA torch slipped in.

A typical slim Dockerfile pattern (illustrative — `Dockerfile.example` at the
repo root is the maintained version):

```dockerfile
FROM python:3.11-slim

# System binaries DocIngest can use when present (all optional)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice ffmpeg exiftool \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch FIRST — use --index-url (NOT --extra-index-url) so the CUDA
# wheel from PyPI can't sneak in. docling reuses this CPU torch instead of
# dragging the ~5.6GB CUDA build.
RUN pip install --no-cache-dir torch torchvision \
    --index-url https://download.pytorch.org/whl/cpu

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .
COPY src ./src
```

Multi-stage builds, BuildKit cache mounts, and uv all work — DocIngest
doesn't care, only the layer ordering does (install deps before copying
mutable source so rebuilds reuse the pip cache layer).

### Non-root containers — pre-download OCR models

**Problem**: docling's RapidOCR backend downloads `.onnx` model files
into the rapidocr package directory inside `site-packages`. On a non-root
container, that directory is read-only → `PermissionError` at first
PDF parse → silent failure (the file is skipped, not the run).

**Solution**: download models in the build stage, point DocIngest at
the result.

```dockerfile
RUN pip install rapidocr  # installs the CLI alongside the library
RUN rapidocr download_models --config /tmp/rapidocr.yaml \
    && mv /tmp/rapidocr-models /app/models/rapidocr
```

```yaml
# docingest.yaml (project root or mounted)
parsing:
  ocr:
    rapidocr_model_paths:
      det: /app/models/rapidocr/ch_PP-OCRv4_det_mobile.onnx
      cls: /app/models/rapidocr/ch_ppocr_mobile_v2.0_cls_mobile.onnx
      rec: /app/models/rapidocr/ch_PP-OCRv4_rec_mobile.onnx
```

DocIngest validates these paths at construction time — partial config
(one path set, others null) raises `ValueError` rather than silently
falling back to the broken default location. See `default.yaml` for
the field-level explanation.

### Cache persistence

DocIngest's incremental cache lives at `{output.dir}/{incremental.cache_dir}/`
by default (so `./kb/.cache/` for `output="./kb/"`). Containers that wipe
their writable layer on restart lose the cache → next run re-pays every
Vision API call.

Two patterns:

1. **Mount a persistent volume at `output.dir`** — simplest, keeps
   everything (sources / chunks / cache) together.
2. **Override only the cache** when `output.dir` must stay ephemeral:
   ```bash
   export DOCINGEST__incremental__cache_dir=/mnt/persistent/docingest-cache
   ```
   Absolute paths skip the `output.dir` prefix (per `pathlib.Path /` semantics
   on POSIX — on Windows this can interact with drive letters; test).

### Cross-container / cross-process file handoff

**Failure mode**: API container writes a file to `/tmp/x.pdf` and puts the
path on a queue. A worker container picks the message up and calls
`docingest.ingest("/tmp/x.pdf")`. **Each container has its own `/tmp`** —
the worker never sees the file. DocIngest reports `failed=1` with
`error_type="io_error"` and `reason="not_found"` (as of v0.2; older
versions silently produced `successful=0` with no errors at all, which is
why this trap existed long enough to bite teams in production).

**Root cause**: Local-FS paths are process-private. Containers, pods,
serverless functions, and even threads-with-different-cwd can disagree
about what `/tmp/x.pdf` resolves to.

**Three correct patterns** — pick the one that matches your platform:

1. **Shared volume** (Azure Files, EFS, NFS, hostPath, persistent disk
   mounted into both pods). Simplest. API writes to the shared mount,
   payload carries the mount-relative path, worker reads from the same
   mount. Works for both files and the `output.dir` cache.

2. **Object storage with byte transfer** (S3, Azure Blob, GCS). API
   uploads bytes, payload carries a blob ID, worker downloads bytes to
   its own `tempfile.mkdtemp()`, calls `ingest()`, cleans up. Most
   portable across platforms; the standard pattern.

3. **Pre-signed HTTPS URL** (often called "presigned URL" or "SAS URL").
   API generates a short-lived signed URL, payload carries the URL,
   worker passes the URL **directly to** `docingest.ingest([url], ...)` —
   DocIngest already supports HTTPS inputs via yt-dlp / direct HTTP GET.
   Most elegant: no temp file on the worker, no extra credential plumbing.

**Don't do**:
- API writes to its own `/tmp` and puts the local path on a queue.
- Two workers ingest different files into the same `output_dir` concurrently
  (no internal locking; aggregates clobber each other).

DocIngest's `inspect()` function will also surface `format="invalid"`
entries with `error_reason="not_found"` for unresolvable inputs — running
inspect before ingest is a cheap pre-flight that catches this trap before
LLM calls start.

### Subprocess isolation (when timeouts matter)

DocIngest links C extensions (docling, pymupdf, onnxruntime). A pure-
Python `threading` cancellation cannot interrupt them mid-call — a hung
PDF can hang the whole host process indefinitely.

If your host is a long-lived service and you need a hard wall-clock cap
beyond `parsing.timeout_sec`, isolate DocIngest in a child process:

```python
import multiprocessing as mp

def _run(paths, output_dir, conn):
    import docingest
    result = docingest.ingest(paths, output=output_dir, outputs=["markdown"])
    # Send via Pipe, not Queue — Queue's feeder thread can keep the
    # process alive after the worker returns, breaking is_alive() polling.
    conn.send({"stats": result.stats, "markdown_files": result.markdown_files})

parent, child = mp.Pipe(duplex=False)
proc = mp.get_context("spawn").Process(target=_run, args=(paths, out, child))
proc.start()
if not parent.poll(timeout=1800):     # 30-min cap
    proc.kill()
    raise TimeoutError("DocIngest hung")
result = parent.recv()
```

`mp.Pipe(duplex=False)` is deliberate — `mp.Queue` carries a background
feeder thread that joins on process exit, which can defeat the
`is_alive()` polling pattern when DocIngest returns a 100KB+ payload.

### Logging

DocIngest writes to standard Python `logging` under the `docingest` and
`docingest.pipeline` logger names. No handlers are installed by the
library — attach your own:

```python
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logging.getLogger("docingest").setLevel(logging.INFO)  # or DEBUG
```

In a subprocess isolation setup, ship logs back to the parent yourself
(the child's stdout/stderr may be invisible to your container platform):
capture in a `StringIO` handler, send through the same `Pipe`, replay
on the parent.

Hook exceptions are caught and downgraded to `warning` level so one bad
hook can't break the pipeline. Set the logger to `DEBUG` to see the full
tracebacks during integration work; switch back to `INFO` in prod.

### Cloud LLM providers

See [README → Python Library](../README.md#python-library) for one-block
examples of `AzureOpenAIProvider` / `BedrockProvider` / `VertexAIProvider`
/ `GeminiProvider` / `OpenAIProvider` / `AnthropicProvider`. The Provider
classes are intentionally thin — they shape a config dict and write the
relevant env vars before the litellm call. Adding a new cloud is one
dataclass + one entry in `_PROVIDER_EXTRA_ENV_MAP` (see
`src/docingest/models/provider.py`).

For a cloud not yet exposed as a Provider class, pass a raw dict:

```python
docingest.ingest(..., vision={
    "primary": {"provider": "groq", "model": "...", "api_key": "..."},
})
```

Anything litellm understands works.

### Observability checklist

When DocIngest looks like it "did nothing" or "did the wrong thing":

1. `result.stats["successful"] vs ["failed"]` — file-level outcome
2. `result.stats["errors"]` — per-file `error_type` (matchable, not freeform)
3. `result.stats["warnings"]` — non-fatal compromises (page cap hit,
   OCR engine downgraded, ...) — these are the silent-correctness traps
4. `result.stats["safety"]` — pre-run budget check; `aborted: true` means
   nothing ran
5. `result.stats["token_usage"]` — LLM cost breakdown by model
6. `docingest doctor` — shows which LLM/cost switches are currently ON

`successful == N AND warnings == []` is the invariant for "everything
finished cleanly".

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
- **Install / CLI / YAML examples** → [README.md](../README.md)
- **Agent-side workflow advice** → [AGENTS.md](AGENTS.md)
