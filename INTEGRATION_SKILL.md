---
name: docingest-integration
description: How to embed DocIngest into another system without stepping
  on the common deployment traps. Read this BEFORE writing Dockerfiles,
  spawning DocIngest subprocesses, or configuring cloud LLM credentials.
---

# DocIngest Integration Skill (for Agents)

You are about to integrate DocIngest into a host application. This skill
captures the decisions other integrators have already made — and the
costs they paid to learn them. It is intentionally a **decision frame**,
not a recipe: the right config depends on your host's constraints, not
on a one-size-fits-all template.

For the underlying mechanics, follow the links to [README.md](README.md),
[INTEGRATION.md](INTEGRATION.md), and [ARCHITECTURE.md](ARCHITECTURE.md).
Don't paraphrase those — link them and add value on top.

---

## 30-second decision tree

| If the host is... | Use | Why |
|---|---|---|
| A Python app, single-tenant, OK with sync calls | Python library — `import docingest; docingest.ingest(...)` | Lowest friction |
| A long-lived web service / worker / API | Library + **subprocess isolation** (see Trap 2) | C extensions ignore `threading` cancellation |
| Non-Python (Node / Go / Bash / CI) | CLI subprocess + `--json` | JSON on stdout, banner on stderr |
| An AI agent (Claude Desktop / Cursor / Copilot) | MCP server — `python -m docingest.mcp_server` | Tool docstrings drive agent behaviour |

---

## Five questions to answer BEFORE writing code

1. **Which LLM provider?** OpenAI direct / Azure / Bedrock / Vertex / Gemini
   / Anthropic? → Pick the matching Provider class from
   [README → Python Library](README.md#python-library). Don't roll your own
   credential plumbing — DocIngest already mirrors the right env vars per
   provider.

2. **Containerised? Non-root user?** → If yes, you need
   `parsing.ocr.rapidocr_model_paths` pre-set (see
   [INTEGRATION → Deployment](INTEGRATION.md#deployment)). Skipping this
   is the #1 silent-failure mode.

3. **How big is the largest doc?** → Always run
   `docingest.inspect(paths)` first on unknown / large inputs. Vision is
   one API call per page; a 300-page PDF can quietly cost tens of dollars.

4. **Is the cache directory persistent across restarts?** → If `output.dir`
   lives on ephemeral container storage, override
   `incremental.cache_dir` to a mounted volume — otherwise every restart
   re-pays for every Vision call.

5. **Do you need progress streaming?** → Library: `on_progress=...`
   callback. CLI: `--json` (one final JSON; for live progress, scrape
   stderr). MCP: single response — no streaming.

---

## Common traps (real ones, with citations)

### Trap 1: "DocIngest returned success but the text looks wrong"

**Symptoms**: `result.stats["successful"] == 1`, but `markdown_files[0].content`
is empty / extremely short / contains binary-looking garbage.

**Root cause options** (rank by probability for your environment):
1. **RapidOCR couldn't write its model files** → see Trap 4 below.
2. Docling parsed but the OCR engine produced empty pages → check
   `result.stats["warnings"]` (page-cap warnings show up here as of v0.2).
3. The file was actually 2 bytes / corrupt / wrong format — check
   `result.markdown_files[i]["metadata"]["format"]`.

**Action**: Always inspect `result.stats["warnings"]` before treating
a successful run as clean. The invariant for "everything completed
correctly" is `successful == N AND warnings == []`.

### Trap 2: C extensions ignore `threading.cancel()`

**Symptoms**: A `concurrent.futures.ThreadPoolExecutor` task hangs on
a malformed PDF; `future.cancel()` does nothing.

**Root cause**: DocIngest calls into docling / pymupdf / onnxruntime
(C extensions). Python's threading cancellation only works at bytecode
boundaries.

**Action**: For wall-clock guarantees beyond `parsing.timeout_sec`, run
DocIngest in a child process. See
[INTEGRATION → Subprocess isolation](INTEGRATION.md#subprocess-isolation-when-timeouts-matter)
for a working `Pipe`-based pattern. **Use `Pipe`, not `Queue`** —
`multiprocessing.Queue`'s feeder thread can keep the child alive after
its work is done, breaking `is_alive()` polling.

### Trap 3: Image is 8–11 GB

**Symptoms**: `docker build` produces an enormous image; `pip` pulled
gigabytes of `nvidia-*` wheels.

**Root cause**: Old docling (<2.92) hard-required torch; even on docling
>=2.95, the default `pip install` may resolve CUDA torch wheels unless
you steer it.

**Action**: Decide whether you need local layout models. Vision-only
deployments need neither torch nor CUDA. See
[INTEGRATION → Image size](INTEGRATION.md#image-size--decide-what-you-actually-need)
for the install-command matrix.

### Trap 4: Non-root container can't OCR

**Symptoms**: PDFs in particular return short/empty markdown; logs show
`PermissionError` near `rapidocr/models/`.

**Root cause**: RapidOCR downloads `.onnx` files to its package directory
inside `site-packages` at first use. Read-only on non-root containers.

**Action**: Pre-download in the build stage, set
`parsing.ocr.rapidocr_model_paths.{det,cls,rec}` to the resulting paths.
See [INTEGRATION → Non-root containers](INTEGRATION.md#non-root-containers--pre-download-ocr-models).
DocIngest validates all three are set together — partial config is an
explicit error, not a silent fallback.

### Trap 5: Subprocess logs vanish

**Symptoms**: Child process (Trap 2 pattern) "succeeded" but the host
platform's log view (`az logs`, `kubectl logs`, etc.) shows nothing
between "started" and "finished".

**Root cause**: `multiprocessing.spawn` does not always forward stdout/
stderr to the parent's streams; container platforms only see the parent's
streams.

**Action**: Capture child logs in a `StringIO` handler attached to the
`docingest` logger, ship them through the same `Pipe` as the result,
re-emit on the parent under a clear prefix. See
[INTEGRATION → Logging](INTEGRATION.md#logging).

### Trap 6: "I changed config but output is the same"

**Symptoms**: Edited `docingest.yaml` (or passed `config_overrides`), but
the second run produces byte-identical output.

**Root cause**: The incremental cache only invalidates on changes to
config keys in `_RELEVANT_CONFIG_PATHS` (see
[ARCHITECTURE.md §7.4](ARCHITECTURE.md)). Unlisted keys don't trigger
re-processing.

**Action**: First check whether your knob is in that whitelist. If it
truly affects output but isn't whitelisted, that's a DocIngest bug —
file it. As an escape hatch, `force=True` rebuilds everything (use
sparingly, it's expensive).

### Trap 9: "I passed outputs=['chunks'] but sources/*.md is still on disk"

**Symptoms**: You asked for chunks only — the disk shows
`sources/*.md`, `index.json`, and possibly `assets/` too.

**Root cause**: `outputs=` controls which **stages** run AND which
artefacts get read back into `IngestResult`. But `sources/*.md` and
`index.json` are **always written** — they're inputs to the incremental
cache and to other enabled stages. The reader for them is skipped (they
won't appear in `result.markdown_files` / `result.index`), but the files
exist.

**Action**:
- If you only need `result.chunks` in memory: this is fine — ignore the
  on-disk files, they cost little.
- If you genuinely need a clean disk: run into a `tempfile.mkdtemp()`
  and `shutil.rmtree()` after consuming the result.
- See [INTEGRATION → Output whitelist](INTEGRATION.md#output-whitelist-the-biggest-perf-knob)
  for the full "always written / toggleable" table.

What `outputs=["chunks"]` **does** save: the knowledge_map LLM call,
quality_report scanning, run_log appending. That's the real cost lever,
not the markdown bytes on disk.

### Trap 8: "successful=0, no errors, no logs" — cross-container path

**Symptoms**: Batch ingest returns `total_files=N` (matches what you
submitted), but `successful=0` and `failed=N` with
`error_type="io_error"`, `reason="not_found"` on every entry. On older
DocIngest versions (<0.2): even worse — `successful=0` with **empty
errors**.

**Root cause**: The path you passed doesn't exist in the calling
process's filesystem. Most common in production: API container writes
`/tmp/x.pdf`, queues the path, worker container reads the queue — but
the two containers have separate `/tmp` filesystems.

**Action**:
- Use one of the three correct patterns in
  [INTEGRATION → Cross-container handoff](INTEGRATION.md#cross-container--cross-process-file-handoff):
  shared volume, object storage byte transfer, or pre-signed HTTPS URL
  passed directly to `ingest([url], ...)`.
- Run `inspect(paths)` before `ingest(paths)` in CI / staging — it
  returns `format="invalid"` entries with `error_reason="not_found"` for
  unresolvable inputs **before** any LLM call. This is the cheapest
  pre-flight catch for path bugs.

### Trap 7: Subprocess hangs on `Queue.put` of large markdown

**Symptoms**: Child process returns, `is_alive()` keeps reporting True
indefinitely.

**Root cause**: `multiprocessing.Queue` has an internal feeder thread
that asynchronously writes to the pipe. A 100KB+ `put` keeps the feeder
alive past the worker's exit.

**Action**: Use `multiprocessing.Pipe(duplex=False)` for result transfer.
Synchronous, no feeder thread, exit is clean.

---

## What you MUST NOT do

- ❌ **Don't share an `output_dir` between concurrent `ingest()` calls.**
  No internal locking; second writer clobbers `chunks.jsonl` / `index.json`.
  Each call gets its own directory.
- ❌ **Don't import from `docingest.pipeline` / `.parsers` / `.chunkers`**
  in consumer code. They are internal — public surface is whatever
  `docingest/__init__.py` re-exports.
- ❌ **Don't pass `force=True` "to be safe".** Incremental cache is
  content-addressed and self-invalidating. Forcing rebuilds on a 1000-file
  corpus is expensive and almost never necessary.
- ❌ **Don't `install_signal_handler=True` in a long-running service.**
  That mode is for stand-alone CLI runs. In a worker it competes with
  the host's SIGTERM handling.
- ❌ **Don't log full API keys** — even at DEBUG level. Forward bool /
  `"(unset)"` markers for diagnostics.
- ❌ **Don't catch `KeyboardInterrupt` and retry.** A user hitting Ctrl+C
  in your CLI tool wants to stop — respect it.
- ❌ **Don't write your own Provider class** before checking whether the
  cloud is supported via a raw dict. `vision={"primary": {"provider":
  "<litellm name>", ...}}` works for anything litellm supports.

---

## Quick reference — where each fact lives

| Topic | Source |
|---|---|
| Public API surface (function signatures, return shapes) | `docingest.{ingest,inspect,refine}` docstrings + [README → Python Library](README.md#python-library) |
| Every config knob + comment | [config/default.yaml](config/default.yaml) |
| Phase ordering, hooks, extension points | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Integration scenarios (FastAPI / MCP / shell / daemon) | [INTEGRATION.md](INTEGRATION.md) — "Scenarios" |
| Container/cloud deployment | [INTEGRATION.md](INTEGRATION.md) — "Deployment" |
| Provider cheatsheet (Azure / Bedrock / Vertex / OpenAI / Gemini / Anthropic) | [README → Python Library](README.md#python-library) |
| Cost-incurring switches audit | `docingest doctor` |

If you need a fact this skill doesn't cover, **read the source of truth
directly**. Don't guess — the API is moving and stale paraphrasing rots
fast.
