# Integrating DocIngest

How to embed DocIngest in your own system. **Scenario-driven, intentionally non-exhaustive** — this is the map, not the manual. Field-level details live in the source of truth: docstrings, [`config/default.yaml`](../config/default.yaml), [README.md](../README.md). Recipes here are starting points, not contracts.

---

## Pick an integration mode

| Mode | Entry point | Best for | Progress | Process model |
|---|---|---|---|---|
| **CLI subprocess** | `docingest run / inspect / refine` | Shell pipelines, non-Python callers, one-shot batch | stderr (banner) + `--json` on stdout | Separate process |
| **Python library** | `import docingest; docingest.ingest(...)` | Embedded in a Python app — RAG pipelines, web backends, workers | `on_progress` callback | In-process (sync) |
| **MCP server** | `python -m docingest.mcp_server` | LLM agents (Claude Desktop / Cursor / Copilot) | Single response per call | Separate process, stdio/SSE |

Public API surface = exactly what `docingest/__init__.py` re-exports (`ingest` / `inspect` / `refine` / `list_knowledge` / `get_summary` / `IngestResult` / `build_config` / Provider classes). Everything else is internal.

**Optional layers** (opt-in, not auto-imported, separate extras): `docingest.graph` (GraphRAG) and `docingest.postprocess` (`docingest extract` — template-driven structured extraction). See [README.md](../README.md) and [ARCHITECTURE.md §9–§10](ARCHITECTURE.md).

---

## Before you write code — decision frame

A **decision frame, not a recipe**: the right config depends on your host's constraints. Decide *what* to do here; the *how* (working code) lives in [README.md](../README.md).

### 30-second decision tree

| If the host is... | Use | Why |
|---|---|---|
| Python app, single-tenant, OK with sync | Python library — `docingest.ingest(...)` | Lowest friction |
| Long-lived web service / worker / API | Library + **subprocess isolation** (Trap 2) | C extensions ignore `threading` cancellation |
| Non-Python (Node / Go / Bash / CI) | CLI subprocess + `--json` | JSON on stdout, banner on stderr |
| AI agent (Claude Desktop / Cursor / Copilot) | MCP server | Tool docstrings drive agent behaviour |

### Five questions to answer first

1. **Which LLM provider?** OpenAI / Azure / Bedrock / Vertex / Gemini / Anthropic → pick the matching Provider class from [README → Python Library](../README.md#python-library). Don't roll your own credential plumbing — DocIngest mirrors the right env vars per provider.
2. **Containerised, non-root?** → you need `parsing.ocr.rapidocr_model_paths` pre-set (Trap 4 — the #1 silent-failure mode).
3. **How big is the largest doc?** → always `docingest.inspect(paths)` first on unknown/large inputs. Vision is one API call per page; a 300-page PDF can quietly cost tens of dollars.
4. **Is the cache dir persistent across restarts?** → if `output.dir` is ephemeral container storage, override `incremental.cache_dir` to a mounted volume, or every restart re-pays every Vision call.
5. **Need progress streaming?** → Library: `on_progress=` callback. CLI: `--json` (one final blob; scrape stderr for live). MCP: single response, no streaming.

### Common traps (real, with the fix pointer)

| # | Trap | Fix |
|---|---|---|
| 1 | **"success but text looks wrong"** — `successful==1` but content empty/garbage | Clean-run invariant is `successful==N AND warnings==[]`. Always inspect `stats["warnings"]` before trusting success. Top cause: Trap 4 |
| 2 | **C extensions ignore `threading.cancel()`** — a task hangs on a malformed PDF, `future.cancel()` does nothing (docling/pymupdf/onnxruntime are C ext) | For wall-clock guarantees beyond `parsing.timeout_sec`, run in a **child process**. Use `Pipe`, not `Queue` (Trap 7) |
| 3 | **image is 8–11 GB** — `pip` pulled `nvidia-*` wheels | Vision-only needs neither torch nor CUDA. Steer the install — see [README → Install](../README.md) (CPU-torch-first) |
| 4 | **non-root container can't OCR** — PDFs return empty markdown, `PermissionError` near `rapidocr/models/` | Pre-download `.onnx` in build stage + set `parsing.ocr.rapidocr_model_paths.{det,cls,rec}`. Partial config is an explicit error, not a fallback |
| 5 | **subprocess logs vanish** — child "succeeded" but `kubectl logs` empty (`mp.spawn` doesn't forward child stdout) | Capture child logs in a `StringIO` handler on the `docingest` logger, ship via the same `Pipe`, re-emit on parent |
| 6 | **"changed config, same output"** | Cache only invalidates on `_RELEVANT_CONFIG_PATHS` keys ([ARCHITECTURE.md §7](ARCHITECTURE.md)). If your knob truly affects output but isn't whitelisted, that's a bug. `force=True` is the (expensive) escape hatch |
| 7 | **subprocess hangs on `Queue.put` of large markdown** — `is_alive()` stays True forever (Queue feeder thread outlives worker on 100KB+ put) | Use `multiprocessing.Pipe(duplex=False)` — synchronous, no feeder thread, clean exit |
| 8 | **"successful=0, no errors" (cross-container)** — `error_type="io_error"`, `reason="not_found"` | Path doesn't exist in the calling process's FS (API container `/tmp` ≠ worker `/tmp`). Hand off via shared volume / blob storage; `inspect()` before `ingest()` as pre-flight |
| 9 | **"`outputs=['chunks']` but `sources/*.md` still on disk"** | `outputs=` controls which **stages** run + what's read back, but `sources/*.md` + `index.json` are **always written** (cache/downstream need them). For a clean disk, run into a tempdir + rmtree. What it really saves: knowledge_map LLM call + quality_report + run_log |

### What you MUST NOT do

- ❌ **Share an `output_dir` between concurrent `ingest()` calls** — no internal locking, second writer clobbers. One dir per call.
- ❌ **Import from `docingest.pipeline` / `.parsers` / `.chunkers`** — internal; public surface is only `__init__.py` re-exports.
- ❌ **Pass `force=True` "to be safe"** — cache is content-addressed + self-invalidating; forcing a 1000-file rebuild is expensive.
- ❌ **`install_signal_handler=True` in a long-running service** — that's for stand-alone CLI; in a worker it fights the host's SIGTERM.
- ❌ **Log full API keys** (even at DEBUG) — forward bool / `"(unset)"` markers.
- ❌ **Catch `KeyboardInterrupt` and retry** — a user hitting Ctrl+C wants to stop.
- ❌ **Write your own Provider class** before trying the raw-dict path: `vision={"primary": {"provider": "<litellm name>", ...}}` works for anything litellm supports.

---

## Scenarios (navigation — working code in README)

Each scenario's full code lives in [README → Python Library](../README.md#python-library) / [MCP Server](../README.md#mcp-server-for-ai-agents). Here's which to use and the one thing to get right.

| Scenario | Use when | Key point | Code |
|---|---|---|---|
| **Backend RAG batch** | one-shot batch → vector store | `outputs=["chunks"]` skips knowledge_map/quality_report LLM cost | [README §Python Library](../README.md#python-library) |
| **Web service + progress** | streaming progress to a UI/SSE | `on_progress=` callback; `raise_on_failure=True` to fail loud; run on a worker thread (Trap 2) | [README §Python Library](../README.md#python-library) |
| **Long-running daemon** | embedded in a server | `install_signal_handler=False` (default); subprocess-isolate per Trap 2 | [README §Python Library](../README.md#python-library) |
| **Agent via MCP** | Claude Desktop / Cursor / Copilot | tool docstrings drive behaviour; client config (`.mcp.json` etc.) + per-call `config_overrides` | [README §MCP Server](../README.md#mcp-server-for-ai-agents) |
| **Refine for humans** | readable/published copy (NOT a RAG step) | `--skill refine_default\|refine_faithful\|refine_html`; raw `sources/*.md` is what RAG consumes | [README §Refine](../README.md) |
| **CLI in shell** | language-agnostic / CI | `--json` on stdout, banner on stderr; `--engine` to switch backend | [README §Usage](../README.md) |

---

## Cross-cutting concerns

Apply to all three modes.

- **Configuration** — three levers, precedence high→low: per-call (`config_overrides=` / CLI args) > `DOCINGEST__*` env vars > project `docingest.yaml` > `config/default.yaml`. Full reference: [`config/default.yaml`](../config/default.yaml) (every knob commented).
- **Credentials** — Provider class (`docingest.GeminiProvider(api_key=...)`) / env var / `config_overrides`. Pick one. Never logged.
- **Output whitelist** — the **biggest perf knob**. `outputs=` decides which stages run + what's read back. `["markdown"]` (clean MD only) / `["markdown","chunks","index"]` (RAG) / etc. Excluded artefacts are not produced, OR produced-then-deleted for runtime deps (`index`/`assets`). `.cache/` always survives. See [README → Python Library](../README.md#python-library) for the full table + `purpose=` presets.
- **Error classification** — every failure carries `error_type` (`timeout` / `parse_error` / `io_error` / `encrypted` / `vision_systemic_failure` / ...). Branch on the token, not the message. Entries in `result.stats["errors"]`.
- **Graceful interrupt** — CLI: Ctrl+C between files finishes the current one then writes partial outputs (exit 130); twice = hard exit. Library: opt in via `install_signal_handler=True` (stand-alone only).
- **Concurrency** — parsing is serialized by design (avoid docling OOM); the win is Vision I/O overlap (`performance.file_concurrency`). Never share an `output_dir` across concurrent calls.
- **Cost control** — `safety.mode` (`off` / `warn` / `strict`). Strict aborts over-budget runs (exit 2); proceed with `--yes` (CLI) / `acknowledge_large=True` (lib/MCP). Tune under `safety:` in [`config/default.yaml`](../config/default.yaml).

---

## Deployment (pointers)

Each is a known production concern; the working config/code is in the referenced file.

- **Image size** — torch is the only thing that matters. Install CPU-torch FIRST (`--index-url`, not `--extra-index-url`) so docling reuses it instead of dragging the ~5.6GB CUDA build. Template: [`Dockerfile.example`](../Dockerfile.example); validate with [`scripts/verify_deps.py`](../scripts/verify_deps.py) (non-zero exit = missing dep, CI gate).
- **Non-root containers** (Trap 4) — RapidOCR writes `.onnx` to its package dir (read-only on non-root). Pre-download in the build stage as root, then set all three `parsing.ocr.rapidocr_model_paths.{det,cls,rec}`. See [`Dockerfile.example`](../Dockerfile.example) (OCR model pre-download layer) + the `parsing.ocr` section of [`config/default.yaml`](../config/default.yaml).
- **Cache persistence** — mount `output.dir` (or override `incremental.cache_dir`) to durable storage, or every restart re-pays Vision (Q4 above).
- **Cross-container handoff** (Trap 8) — API and worker must share the actual bytes: shared volume, or write to blob storage and pass a path the worker can resolve. `inspect()` before `ingest()` catches a wrong path cheaply.
- **Subprocess isolation** (Trap 2/7) — for hard wall-clock limits, run `ingest()` in a child process; return results via `multiprocessing.Pipe(duplex=False)` (not `Queue`). Capture child logs via a `StringIO` handler on the `docingest` logger and ship them through the Pipe (Trap 5).
- **Cloud LLM providers** — Azure / Bedrock / Vertex via the matching Provider class, or the raw-dict path (`vision={"primary": {"provider": "<litellm name>", ...}}`). Shapes + required fields: [README → Python Library](../README.md#python-library) + `docingest/providers.py` docstrings.
- **Observability** — `docingest doctor` (env / deps / keys) is the first debug step. Then check `result.stats` (`warnings`, `errors`, `quality`, `token_usage`).

---

## Pointers (real source of truth)

- **Field-level config** → [`config/default.yaml`](../config/default.yaml) (every knob has an inline comment)
- **API signatures / behaviour** → docstrings on `docingest.ingest` / `inspect` / `refine` / `run_pipeline`
- **Internal architecture** (Phase / hooks / parsers / chunkers / extension points) → [ARCHITECTURE.md](ARCHITECTURE.md)
- **Install / CLI / YAML / Docker examples** → [README.md](../README.md)
- **Agent-side workflow advice** → [AGENTS.md](AGENTS.md)
