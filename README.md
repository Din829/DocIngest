# DocIngest

Universal document preprocessing for RAG and Agentic Search.

Any input — PDF, Office, HTML, images, audio, video, ZIP, http(s) URLs — becomes clean
Markdown + chunks + a searchable index. One pipeline, two consumers: **RAG** (vector
search over `chunks.jsonl`) and **Agentic Search** (grep/read over `sources/*.md`).

**DocIngest does not retrieve.** No embeddings, no vector search, no answer generation —
it prepares data; you search it with your own tools. That boundary is deliberate.

## Quick start

```bash
docingest doctor                        # 1. is the environment ready?
docingest inspect ./docs/               # 2. how big / how much will it cost?
docingest run ./docs/ -o ./kb/          # 3. process
```

Then read `./kb/knowledge_search.SKILL.md` (an auto-generated search guide for this
corpus) and grep `./kb/sources/*.md`, or feed `./kb/chunks.jsonl` to your vector store.

Re-running `run` on the same output is cheap — unchanged files are skipped by content
hash.

## What a run produces

Everything lands under the output directory:

| Path | What it is |
|---|---|
| `sources/*.md` | Clean Markdown with YAML frontmatter — the product. Grep/read these. |
| `chunks.jsonl` | Chunked text + metadata, one JSON object per line. Feed to your embedder. |
| `index.json` | File directory for agent discovery, plus per-file PDF element bounding boxes. |
| `knowledge_map.yaml` | Corpus summary + keyword reverse index (machine-readable). |
| `knowledge_search.SKILL.md` | Search guide for this corpus: file table, keyword index, search protocol. **Read this first.** |
| `quality_report.json` | Vision accuracy self-check — scans output for `[?]` / `[unreadable]` markers. |
| `assets/` | Rendered page images and extracted embedded images. |
| `log.md` | Append-only run history (`run_log` output; disable via `outputs`). |
| `run.log` | Full INFO-level log of the last run. Always written. |
| `errors.json` | Written only when files fail. Each entry has `file` / `error` / `error_type`. |
| `.cache/` | Incremental cache. Never delete it unless you want a full rebuild. |

Optional, produced by follow-up commands: `readable/` (`refine`), `graph/` +
`chunks_enriched.jsonl` (`graph build`), `extracted/<template>.jsonl` (`extract`),
`viz/` (`visualize`).

<details>
<summary><b>Open Knowledge Format (OKF) compatibility</b></summary>

`sources/*.md` frontmatter is field-level compatible with
[Google Open Knowledge Format v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md),
so OKF-aware consumers (OpenWiki, knowledge catalogs) can read the output directly:
`type` (OKF's only required field, derived from format), `title`, `tags`, `resource`
(canonical source URI for URL inputs), and `related` links in OKF §5.1 bundle-relative
form. `description` — one retrieval-optimized sentence per file — is opt-in
(`output.derived_metadata.description.enabled`, ~1 LLM call per 20 files) and
**recommended for multi-file knowledge bases**: in a 10-file cross-domain benchmark, an
agent picking which file to open from frontmatter alone went from 64% (title+tags) to
89% (+description).

DocIngest deliberately does not emit a full OKF bundle — `index.json` is its own
machine-readable contract. Compatibility is per-file frontmatter semantics, which is
what retrieval-side consumers actually read.
</details>

## Commands

Flags below are the ones you'll actually reach for. **`docingest <cmd> --help` is the
source of truth** — run it when in doubt.

| Command | Does | Key flags |
|---|---|---|
| `run <inputs...>` | Documents → Markdown + chunks + index | `--mode fast\|balanced\|best` · `-o/--output` · `--purpose` · `--outputs` · `--strategy` · `--max-pages N` · `--parallel-files N` · `--force` · `--sync` · `-y` · `--json` · `-v` |
| `inspect <inputs...>` | Size / pages / cost estimate — **no parsing** | `--json` |
| `extract <kb>` | Fill a YAML-declared schema per document → `extracted/<template>.jsonl` | `-t/--template` · `--input sources\|chunks` · `--parallel N` · `--json` |
| `refine <files...>` | Rewrite Markdown into a human-readable copy (LLM) | `--skill refine_default\|refine_faithful\|refine_html` · `-o` · `-y` |
| `doctor` | Check packages, external tools, API keys | — |
| `visualize <kb>` | Draw parse bounding boxes onto page images (QA) | `--pages 1,3` · `--labels table` · `--numbers` |
| `export <kb>` | Push chunks to a vector store (Azure AI Search) | `--endpoint` · `--index` · `--embed-model` · `--embed-dim` · `--vector-field` |
| `skills list` | List the refine styles `refine --skill` accepts | `--json` |
| `graph build\|query\|status\|enrich` | Optional knowledge-graph layer → [GraphRAG](#graphrag-optional) | see that section |

`run` accepts files, directories, ZIP archives, and http(s) URLs — mixed freely in one
call:

```bash
docingest run report.pdf slides.pptx -o ./kb/
docingest run ./docs/ archive.zip "https://www.youtube.com/watch?v=..." -o ./kb/
docingest run meeting.mp3 presentation.mp4 -o ./kb/
```

`-o` is optional for a **single** input (it derives `./knowledge/<input-name>/`) and
**required** for multi-input runs, so separate knowledge bases never silently merge.

**Advanced flags on `run`** — normally covered by `--mode`, set them only for the noted
case: `--engine docling | vision_only | azure_di | docling_with_fallback` (set mainly for
`azure_di`; `--mode fast` already picks `vision_only` for PDF, and non-PDF formats
delegate back to docling under it), `--parallel N` (within-file Vision workers),
`-c/--config <path>` (point at a specific `docingest.yaml`).

**Conventions shared by every command:**

- JSON goes to **stdout**, banner / progress / errors to **stderr** — safe to pipe.
- Exit codes: `0` success · `1` some files failed · `2` safety abort · `130` graceful Ctrl+C.
- Incremental by default. `--force` ignores the cache logic but does **not** delete
  `<output>/.cache/` — for a genuinely clean run, use a new output directory.
- **Vertically typeset CJK PDFs skip Docling.** Layout analysis reads vertical Japanese
  as a table and shreds it one glyph per cell, so a ~0.02 s probe routes those files
  straight to `vision_only`. Measured on a 17-page 法令 PDF: 358 s of unusable
  pseudo-table → **10 s** and a correctly-read scoring table. Both signals must fire
  (single-glyph spans ≥ 90% **and** average span ≤ 20 pt); across 75 real PDFs only the
  genuinely vertical one matched. Tune or disable under `parsing.vertical_detect`.
- **Parse failures fall back instead of losing the file.** When Docling fails on a PDF
  or image (timeout, parser error), DocIngest retries once with the `vision_only`
  engine, which renders pages and lets Vision read them. The downgrade is never
  silent: it appears in the run warnings and in `metadata.lineage.transformations`
  (`step: parse_fallback`), because the recovered file has no per-element bounding
  boxes and every page was billed to Vision. Disable with
  `error_handling.on_parse_failure: skip`.
- Safety is `strict` by default: an over-budget run aborts with exit 2. Review the
  estimate, then re-run with `-y`.

## Processing modes

One flag bundles the cost/quality knobs. The key design point: **the same mode resolves
to a different path per file type**, so each format gets its best route automatically.

| Mode | Use for | What it actually does |
|---|---|---|
| `fast` | bulk first pass, gist only | PDF → `vision_only` (skips Docling parse entirely — big speedup). Office → still Docling, but 64-way concurrency, aggressive triage, figure extraction off. **Also pins Vision to `gemini-3.5-flash-lite`** instead of the default `gemini-3.7-flash` (Lite measured at 14 s vs Flash 44 s on WEO 75p, table structure intact, content −5.7%). |
| `balanced` *(default)* | almost everything | Nothing to pass — this is plain `docingest run`. |
| `best` | contracts, spec sheets, never-miss-a-word | Every page to Vision (triage off) + batched calls off. ~2× cost, zero-miss recall. |

Why `fast` differs per format: `vision_only`'s speed win only holds for PDF, which
PyMuPDF renders quickly. Office formats are bottlenecked on LibreOffice→PDF rendering
that no engine can skip, so their `fast` saves by sending *fewer* Vision calls, not by
switching engines.

An explicit flag (`--engine`, `--parallel`, …) overrides the mode. A misspelled mode
fails loud with the valid names — it never silently runs at the wrong cost point. Full
knob-by-knob matrix: [docs/PROCESSING_MODES.md](docs/PROCESSING_MODES.md); the exact
override sets live in `_MODE_PRESETS` in [`src/docingest/api.py`](src/docingest/api.py).

## Choosing what gets written

Produce exactly the artefacts you want on disk — skipped stages aren't just deleted,
they never run (so you save the LLM cost too).

```bash
docingest run ./docs/ -o ./kb/ --purpose rag        # Markdown + chunks + index
docingest run ./docs/ -o ./kb/ --outputs markdown,chunks
```

| `--purpose` | Expands to |
|---|---|
| `markdown` | `markdown` |
| `rag` | `markdown`, `chunks`, `index` (chunking auto-on) |
| `agentic` | `markdown`, `index`, `knowledge_map` |
| `full` *(default)* | everything |

`--outputs` is the precise form and **overrides `--purpose`**. Valid values, exactly:
`markdown` · `chunks` · `index` · `assets` · `knowledge_map` · `quality_report` ·
`run_log`. (No abbreviations — `md` is not valid.) `index` and `assets` are runtime
dependencies, so they're produced and then deleted when unwanted; `.cache/` always
survives so incremental keeps working. Same names work as `outputs=[...]` in the Python
and MCP paths. Legacy `--no-chunks` still works.

## Recipes

| Goal | Command |
|---|---|
| Ingest a folder, keep it mirrored | `docingest run ./docs/ -o ./kb/ --sync` |
| Cheap first pass over a big pile | `docingest run ./docs/ -o ./kb/ --mode fast` |
| A contract where nothing may be missed | `docingest run deal.pdf -o ./kb/ --mode best` |
| Only the first 20 pages of a huge PDF | `docingest run big.pdf -o ./kb/ --max-pages 20` |
| Markdown only, no RAG artefacts | `docingest run ./docs/ -o ./kb/ --purpose markdown` |
| Faster wall-clock on many files | `docingest run ./docs/ -o ./kb/ --parallel-files 4` |
| Cost estimate for an agent to read | `docingest inspect ./docs/ --json` |
| Any of the above, machine-readable | add `--json` (summary to stdout, progress to stderr) |
| A structured table out of similar docs | `docingest extract ./kb/ -t doc_summary` |
| A human-readable copy of one file | `docingest refine ./kb/sources/spec.md` |
| Check the parse actually got the tables | `docingest visualize ./kb/ --labels table --numbers` |

`inspect --json` returns one object per file — enough for an agent to decide before
spending anything:

```json
[{"name": "nutrition.pdf", "format": "pdf", "size_mb": 0.91, "pages": 10,
  "chars_est": 15983, "est_cost_usd": 0.13, "recommendation": "Ready"}]
```

`--sync` mirrors exactly one directory: after a fully successful run it prunes outputs
owned by source files that disappeared. The first successful sync binds that knowledge
base to that directory; pointing it at another one later is rejected. Cleanup runs only
when every discovered file succeeded — aborts, parse failures, and interrupts preserve
the previous state. Ordinary runs never delete anything, because a normal call may be a
partial import.

`--max-pages N` caps **parsing** (layout + Vision + chunking), and the cost preview
reflects N. It is not `parsing.vision.max_pages`, which parses everything but only
Vision-enriches the first N.

`--parallel-files N` overlaps *files*; `--parallel N` sets the within-file Vision worker
pool. Parsing stays serialized by design — file B parses while file A waits on Vision
I/O, which is where the time goes. Outputs stay input-ordered and byte-identical to a
sequential run.

## Install

**Prerequisites:** Python 3.10+ and git.

Two scripts do the whole install — one for Python packages, one for the system binaries
DocIngest shells out to (LibreOffice / ffmpeg / poppler).

```bash
git clone https://github.com/Din829/DocIngest.git && cd DocIngest
python -m venv .venv && source .venv/bin/activate     # 1. isolated env
./scripts/install_python_deps.sh                       # 2. CPU torch + DocIngest
./scripts/install_system_deps.sh                       # 3. LibreOffice / ffmpeg / poppler
cp .env.example .env                                   # 4. add your API key
python scripts/verify_deps.py                          # 5. gate: non-zero if anything is missing
```

**Windows (PowerShell)** — same five steps with `.ps1`, except step 5 needs a **new**
shell (the system-deps step edits PATH):

```powershell
git clone https://github.com/Din829/DocIngest.git ; cd DocIngest
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
.\scripts\install_python_deps.ps1
.\scripts\install_system_deps.ps1
Copy-Item .env.example .env
# open a NEW PowerShell, re-activate the venv, then:
python scripts\verify_deps.py
```

If PowerShell blocks the script, run
`powershell -ExecutionPolicy Bypass -File .\scripts\install_python_deps.ps1`.

**API keys** — only two, both optional; set what you use:

| Key | For | Needed when |
|---|---|---|
| `GEMINI_API_KEY` | Vision AI (reads charts / scans / images per page) | Almost always — it's the default Vision engine |
| `DASHSCOPE_API_KEY` | Audio/video transcription (Qwen3-ASR) | Only if you ingest audio or video |

Library callers can inject credentials at call time instead — see
[Python library](#python-library).

**`doctor` vs `verify_deps.py`** — `docingest doctor` is a friendly table for humans and
always exits 0. `python scripts/verify_deps.py` is the real gate: non-zero exit when a
required dep is missing or a CUDA torch slipped in. Use it in CI / Docker.

**Optional extras** — add one only when you need that feature; there is deliberately no
"install everything" path:

```bash
pip install -e ".[nlp]"          # Japanese keyword extraction (SudachiPy)
pip install -e ".[mcp]"          # MCP server (FastMCP)
pip install -e ".[audio]"        # Audio transcription (DashScope Qwen3-ASR)
pip install -e ".[azure]"        # azure_di parse engine + export to Azure AI Search
pip install -e ".[langchain]"    # LangChain loader
pip install -e ".[postprocess]"  # Structured extraction marker (no extra deps)
pip install -e ".[graph]"        # GraphRAG layer (LightRAG)
pip install -e ".[graph-local]"  # Local embeddings for graph — adds ~2GB torch libs
pip install -e ".[graph-gemini]" # Gemini embeddings for graph
```

<details>
<summary><b>Why the scripts instead of a plain <code>pip install -e .</code></b> — and why a CUDA torch is a correctness problem, not just a size one</summary>

docling pulls `torch` transitively, and on Linux the default PyPI wheel is the ~5.6GB
**CUDA** build. `install_python_deps` forces the CPU wheel up front. By hand, run the
CPU-torch line first:

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -e .
```

**A CUDA torch changes extraction results, silently.** DocIngest never sets docling's
`accelerator_options`, so its default `device=auto` switches to the GPU as soon as a
CUDA torch is importable — nobody opts in. On Ampere, `torch.backends.cudnn.allow_tf32`
defaults to `True`, and the reduced precision shifts layout-model bounding boxes enough
to drop whole text blocks. Measured (RTX 3080 Ti, torch 2.12, docling 2.113): on a
21-page Japanese document a heading line disappeared on **every** GPU run and on **no**
CPU run (5/5 vs 0/5 — deterministic, not jitter), with nothing in the output marking the
gap. `verify_deps.py` fails the build when it finds a CUDA torch.

Speed was never the objection — the same run measured GPU at 3–7× faster. The project
simply doesn't chase it (see
[COMPETITIVE_POSITIONING.md](docs/COMPETITIVE_POSITIONING.md)), and a default install
shouldn't quietly trade correctness for speed. If you *deliberately* run on GPU, set
`torch.backends.cudnn.allow_tf32 = False` before the first inference — that restored
byte-identical output on all 7 test files at no measurable speed cost.
</details>

<details>
<summary><b>Docker / CI, and embedding DocIngest in another project</b></summary>

`Dockerfile.example` is a single-stage production template wiring these same scripts
together: system bins layer → CPU-torch + pip layer → optional OCR model pre-download →
`verify_deps.py` build gate → non-root user. `install_system_deps.sh` covers apt / dnf /
yum / pacman / zypper / brew; the `.ps1` uses winget plus a manual poppler download.

To vendor DocIngest into another Python project (no package index needed):

```bash
cp -r /path/to/DocIngest ./vendor/DocIngest
pip install -e "./vendor/DocIngest[mcp,audio,nlp]"
docingest doctor
```

System tools and API keys are **per-machine, not per-project** — every host running the
consumer needs them. Keys can be injected via Provider objects so consumers never touch
`.env`. Re-sync the source tree to update; only re-run `pip install -e` if
`pyproject.toml`'s dependency list changed.
</details>

## Configuration

Four layers, highest wins:

```
CLI flags  >  DOCINGEST__* env vars  >  project docingest.yaml  >  config/default.yaml
```

[`config/default.yaml`](config/default.yaml) is the single source of truth — every knob
is there with inline comments explaining why the default is what it is. Drop a
`docingest.yaml` in your project root to override only what you need.

```yaml
chunking:
  strategy: "heading"       # auto | heading | recursive | slide | sheet | timestamp | whole
  max_tokens: 1024

models:
  defaults:                 # ONE model for every text/vision task unless a task overrides
    primary: { provider: "google", model: "gemini-3.7-flash" }
    fallback: { provider: "openai", model: "gpt-5.4-mini" }

parsing:
  vision:
    image_dpi: 200
    triage: { enabled: true }   # skip pure-text pages → 30-60% Vision cost saved

sanitize:
  enabled: false            # PII masking (email / card / IP / phone), default OFF
```

Any value can be overridden by an env var, `__` separating levels:

```bash
export DOCINGEST__chunking__max_tokens=1024
export DOCINGEST__parsing__audio__language=ja
```

**Models.** One model serves every text and vision task (Vision, chunking assist,
knowledge-map summary, refine, graph) — change `models.defaults` once and it changes
everywhere; add a `primary:` under a specific task to override just that one. Two roles
are intentionally separate: `models.audio_transcription` (ASR: `qwen3-asr-flash`,
falling back to `whisper-1`) and `graph.embedding`. There is **no hard-coded model name
in code** — a task with no model fails loud rather than silently substituting one.

**Credentials gotcha:** the CLI calls `load_dotenv()` without `override=True`, so an
API key already present in the **environment wins over `.env`**. A stale env var
therefore doesn't fail loudly — the primary provider errors, the fallback answers, and
the run looks successful. If you suspect this, check `token_usage.by_model` in
`--json` output: it reports the model actually billed.

**Naming trap:** `performance.parallel_files` in YAML is the *within-file* Vision worker
pool (CLI `--parallel`), while CLI `--parallel-files` maps to
`performance.file_concurrency`. Similar names, opposite meanings.

## Python library

The public surface is exactly what `docingest/__init__.py` exports: `ingest`, `inspect`,
`refine`, `list_knowledge`, `get_summary`, `IngestResult`, `build_config`,
`__version__`, and the Provider classes. Everything else under `docingest.*` is
internal and may change between minor versions.

```python
import docingest

result = docingest.ingest("./docs/", output="./kb/")
print(result.stats["successful"], "files processed")
for chunk in result.chunks:
    embed(chunk["text"])
```

`ingest()` keyword arguments: `output`, `outputs`, `purpose`, `vision` / `audio` /
`text` (Provider injection), `config_overrides`, `config_file`, `force`, `mode`,
`acknowledge_large` (the `-y` equivalent), `on_progress`, `install_signal_handler`,
`raise_on_failure`, `sync`.

```python
# Pick outputs, pick a mode, override any YAML value — all per call
docingest.ingest(
    "./docs/", output="./kb/",
    mode="fast",
    outputs=["markdown", "chunks"],
    config_overrides={"parsing.vision.max_pages": 200, "chunking.max_tokens": 1024},
)

# Inject credentials instead of using .env
docingest.ingest(
    "./docs/", output="./kb/",
    vision=docingest.GeminiProvider(api_key="..."),
    audio=docingest.DashScopeProvider(api_key="..."),
)
```

Providers: `GeminiProvider`, `OpenAIProvider`, `AnthropicProvider`, `AzureOpenAIProvider`,
`BedrockProvider`, `VertexAIProvider`, `DashScopeProvider`, `WhisperProvider` (plus the
`VisionProvider` / `AudioProvider` / `TextProvider` base classes for custom subclasses).
Cloud providers follow the same shape; unset optional fields fall back to ambient
credentials (container IAM role, workload identity, gcloud ADC).

```python
docingest.ingest("./docs/", output="./kb/", vision=docingest.AzureOpenAIProvider(
    model="my-gpt4-deployment",          # Azure deployment name, NOT a model id
    api_base="https://my-resource.openai.azure.com/",
    api_version="2024-08-01-preview",
))
```

**`IngestResult`** carries the artefacts so you don't re-read the output directory:
`markdown_files`, `chunks`, `index`, `knowledge_map`, `quality_report` (each populated
when that name is in `outputs`), plus `stats` and `output_dir` which are always present.
`stats` holds `total_files` / `successful` / `failed` / `token_usage` / `errors` /
`warnings` / `quality` / `safety` / `interrupted`. (`run_log` is a valid `outputs` value
— it toggles `log.md` on disk — but it is not a field on the result object.)

**Failure handling** — `ingest()` returns rather than raises; the caller owns errors via
`stats["errors"]` (each entry has `file` / `error` / `error_type`). Failures are always
logged at warning level so the "succeeded, 0 files" trap can't hide. Pass
`raise_on_failure=True` for a `RuntimeError` on any failure.

**Progress** — `on_progress=fn` receives `kind="file_done"` per file and
`kind="file_progress"` for within-file Vision page progress, so a UI bar isn't frozen
during a big file. The callback runs on the pipeline thread and its exceptions are
swallowed (logged) so a buggy callback can't break a run.

**Signals** — as a library, DocIngest installs **no** SIGINT handler, leaving your host's
Ctrl+C handling intact. The CLI opts in (`install_signal_handler=True`).

**Parse timeout scales with size** — `clamp(base + per_page * pages, base, max)`
(defaults 120 / 3 / 1800 s, so a 519-page PDF gets ~1677 s and a 10-page memo ~150 s).
Non-PDFs fall back to the flat `parsing.timeout_sec`. Tune under
`parsing.dynamic_timeout`.

Desktop GUI: `python -m docingest.gui` (needs `pywebview`; `start_gui.bat` on Windows).

## MCP server

```bash
pip install -e ".[mcp]"
python -m docingest.mcp_server                    # stdio (Claude Desktop / Code, Copilot)
python -m docingest.mcp_server --transport http   # streamable HTTP
```

Seven tools, each a thin wrapper over the same Python API: `inspect` · `run` · `refine` ·
`build_graph` · `query_graph` · `graph_status` · `enrich_chunks` (the four graph tools
register only when `[graph]` is installed). All accept `config_overrides`.

Browsing or searching the knowledge base is **deliberately not a tool** — agents use
their own Grep / Read / Glob on the artefacts, starting from the generated
`knowledge_search.SKILL.md`. Client configuration and troubleshooting:
[INTEGRATION.md §4](docs/INTEGRATION.md).

**How agents discover the command set:** one catalog, four channels —
`.claude/skills/docingest/SKILL.md` (Agent Skill, description auto-loads in ~100
tokens), [AGENTS.md](AGENTS.md), the MCP `instructions` block, and `docingest --help`.
`tests/unit/test_command_catalog.py` pins that catalog to the source, so a command can't
change without the table going red.

## Structured extraction (optional)

Where `run` produces *text* and `graph build` produces a *graph*, `extract` produces a
*table*: one strongly-typed record per document, fields exactly as you declared them.
It reads the generated `sources/*.md` (or `chunks.jsonl`) — never the original files.

```bash
docingest run ./docs/ -o ./kb/
docingest extract ./kb/ --template doc_summary
docingest extract ./kb/ -t ./my_template.yaml --input chunks --parallel 8
```

A template is a field table plus rules — change what gets extracted by editing YAML, no
code:

```yaml
name: contract
fields:
  - name: party_a
    type: str
    description: 甲方 / first party
  - name: amount
    type: str
    description: contract amount, keep the original wording
    required: false
  - name: obligations
    type: list[str]
    description: key obligations, max 10
rules:
  - Only extract what the text states; never invent.
```

Built-ins live in `postprocess_templates/`; drop your own there or pass a path. Long
documents are split, processed in parallel, and merged; a per-piece failure is isolated,
not fatal. From Python, `import docingest.postprocess` explicitly (`import docingest`
never triggers it) and call `docingest.postprocess.run(kb, processor="extract",
template=...)`.

## GraphRAG (optional)

An entity/relation knowledge graph over an existing knowledge base, with global / local
/ hybrid queries via [LightRAG](https://github.com/HKUDS/LightRAG). Strictly opt-in —
`run` never touches it, and `docingest.graph` only loads when explicitly imported.

**More expensive than Vision.** Use it for "themes / relationships / multi-hop across
the corpus" questions; for single-fact lookup, plain grep over `sources/*.md` is cheaper
and better.

```bash
pip install -e ".[graph]"
docingest run ./docs/ -o ./kb/
docingest graph build ./kb/                        # full mode (default)
docingest graph build ./kb/ --mode vector_only     # cheap: skip community detection
docingest graph query "What are the main themes?" --kb ./kb/ --mode global
docingest graph status ./kb/
```

| Build mode | Cost | Query modes it allows | Best for |
|---|---|---|---|
| `vector_only` | cheap (no community-summary LLM calls) | `naive`, `local` | single-fact / two-hop |
| `full` *(default)* | higher (per-community LLM summary) | `naive`, `local`, `global`, `hybrid`, `mix` | multi-hop, "main themes" |

Artefacts land in `./kb/graph/`; deleting that folder leaves the rest intact. Extraction
is incremental — chunks whose content + LLM-config hash is unchanged are skipped.
Embedding providers (`OpenAIEmbedding` / `GeminiEmbedding` /
`SentenceTransformerEmbedding`) are independent of the main pipeline's providers, so the
graph can use a small cheap model without affecting `run`.

**Boost ordinary vector RAG with graph entities.** `graph build --enrich-chunks` (or
`graph enrich <kb>` later) writes a sibling `chunks_enriched.jsonl` with each chunk's
top entities injected into both the text (helps embedding recall on synonyms) and
`metadata.entities` (for hybrid / filtered retrieval). **The original `chunks.jsonl` is
never modified** (hash-checked by tests), and no LLM or embedding calls are made — it's
a pure replay over on-disk graph data (~35 ms for 44 chunks). Delete the enriched file
any time to drop back.

**Speed vs. recall — two knobs.** `graph.lightrag.entity_extract_max_gleaning` defaults
to `0` (LightRAG's own default is 1): for structured input — DB sheets, API specs,
contracts — one pass already captures 95%+ of entities, halving the LLM bill. Raise it
to 1 for prose / academic / legal corpora where the first pass misses 10–15% of edge
entities. `graph.lightrag.max_parallel_insert` (default 4) trades RPM for wall-clock.

<details>
<summary><b>Known issue — repeated <code>query()</code> in one Python process</b></summary>

LightRAG 1.4's internal `asyncio.Lock` binds to the first event loop, so a fresh
`asyncio.run()` per call fails on the 2nd+ invocation with "Lock bound to a different
event loop".

| Entry point | Status |
|---|---|
| CLI | ✅ Each invocation is a fresh subprocess — nothing to do |
| MCP server | ✅ Auto-fixed: the MCP entry point applies `nest_asyncio` |
| Python library | 🟡 Not auto-fixed (we won't monkey-patch a host's asyncio). Either call `nest_asyncio.apply()` yourself, or use one subprocess / one call per process |

When it bites, the empty answer surfaces as `result.stats["error"]` with a descriptive
message — detectable programmatically, just not recoverable without the workaround.
</details>

<details>
<summary><b>Credential resolution order (graph layer)</b></summary>

1. Explicit `Provider(api_key="...")`
2. Environment variable (`OPENAI_API_KEY`, `GEMINI_API_KEY`, …)
3. `.env` in the working directory — **CLI path only**
4. YAML config

Library callers do **not** auto-load `.env`, by design: embedding DocIngest in a
long-running host shouldn't pollute the process environment. Call `load_dotenv()`
yourself or pass Provider objects.
</details>

## Other integrations

**LangChain** (`pip install -e ".[langchain]"`) — load a knowledge base's chunks straight
into LangChain instead of re-splitting with a naive character splitter. Since LangChain
already bridges dozens of vector stores, this one adapter connects DocIngest to all of
them. Pulls only `langchain-core`:

```python
from docingest.integrations.langchain import DocIngestLoader
docs = DocIngestLoader("./knowledge/").load()   # -> list[Document]
vectorstore.add_documents(docs)
```

**Azure** (`pip install -e ".[azure]"`) — two independent capabilities; the core pipeline
never imports either. (1) `parsing.engine: azure_di` parses in Azure Document
Intelligence instead of local docling, with zero local parse memory pressure. (2)
`docingest export` embeds chunks with your own model and pushes them into an Azure AI
Search vector index, preserving DocIngest's chunks instead of letting Azure re-chunk:

```bash
docingest export ./kb/ --to azure-search \
    --endpoint https://<svc>.search.windows.net --index my-index \
    --embed-provider azure-openai --embed-model my-embed-deployment --embed-dim 1536 \
    --embed-endpoint https://<res>.openai.azure.com/ --vector-field content_vector
```

Field names and embedding are injectable, and the dimension is verified before upload.
Full detail: [`src/docingest/azure/README.md`](src/docingest/azure/README.md).

## How it works

```
input files / dirs / URLs / ZIPs
      │
      ▼  discover_files (ZIP expansion, yt-dlp for URLs)
      ▼  Phase 0.5: legacy .xls/.doc/.ppt → OOXML via LibreOffice
      ▼  partition by incremental cache (skip unchanged)
      │
      ├─ per new file ─────────────────────────────────────────────┐
      │   pre_parse hook       DOCX OMML → LaTeX                   │
      │   parse                Docling / Media / Text              │
      │     ↳ xlsx pre-route   openpyxl renderer (per-sheet        │
      │                        headings, merged cells anchor-only) │
      │   garbled fallback     glyph< → pymupdf                    │
      │   page render          xlsx/docx/pptx → PDF → screenshots  │
      │   post_parse hook      PPTX chart direct-read              │
      │   Vision enrichment    per-page, 10-layer triage, parallel │
      │   pre_write hook       exiftool / sanitize                 │
      │   write                sources/*.md + assets/              │
      │   chunk + path-inject  auto/heading/slide/sheet/timestamp  │
      └────────────────────────────────────────────────────────────┘
      │
      ▼  index.json + chunks.jsonl + knowledge_map.yaml + quality_report.json
      ▼  (opt-in) graph build → graph/
```

Full phase breakdown, design rationale, and how to add formats / hooks / chunkers:
[ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Capabilities

Everything below is config-driven — the knob for each is in `config/default.yaml`.

**Formats.** 20+ via Docling (PDF, DOCX, PPTX, XLSX, HTML, images, Markdown, …).
Legacy `.xls` / `.doc` / `.ppt` are auto-converted to OOXML via LibreOffice first
(Docling rejects the binary forms outright), with the original filename / mimetype
preserved in `metadata.lineage`. ZIP archives unpack recursively with Japanese filename
recovery and bomb protection. magika identifies files with weak or missing extensions.

**Excel is rendered by openpyxl, not Docling** (default) — every sheet's body sits under
its own `## SheetName` heading so chunk `title_path` always points at the right sheet;
merged cells stay anchor-only instead of duplicating N×N; empty columns are pruned.
Embedded pictures (including EMF/WMF, read straight from OOXML to bypass openpyxl's
silent drop) are anchored to their real row with an `<!-- image: -->` marker so Vision
can pick them up. This is the project's core competence: Japanese 方眼紙 spec sheets.

**Audio & video.** Subtitle-first (SRT/VTT, zero API cost), then Qwen3-ASR-Flash with
Whisper fallback; long audio auto-segmented. For video the default is **native video
understanding** — the whole file goes to a video-capable model in one call, so it watches
frames *and* hears audio together (measured on a 100 s screencast: 1 API call vs 10,
~11K tokens vs ~50K, far fewer `[unreadable]` markers). Gemini-only today; other
providers degrade automatically to frame sampling + ASR. YouTube / Bilibili / 1000+
platforms via yt-dlp.

**Per-page Vision AI.** The model decides per page: clean up text, describe charts, OCR
a scan. Parallel, cached by content hash. **Triage** skips pure-text pages for 30–60%
savings with no information loss, and has ten layers of defence for damaged pages —
including language-script consistency (catches CMap failures that produce *clean but
wrong* Unicode, e.g. Bengali glyphs on a Japanese document) and Latin cipher garble
(detected by an abnormally low vowel ratio). **Anti-hallucination is explicit**: the
model marks `[?]` for partial reads and `[unreadable]` for gaps, and
`quality_report.json` scans the output for them afterwards.

**Format-aware Vision supplement.** For xlsx — already rendered cleanly by openpyxl —
Vision only *supplements* the visual content (charts, stamps, pictures) and never
re-transcribes the table, killing Docling↔Vision duplication at the source (measured:
per-section re-transcription 65–92% → 0–5%). PDF and PPT stay on full whole-page
transcription because Docling fragments their grid tables.

**Chunking.** Strategy auto-selected per format (heading / recursive / slide / sheet /
timestamp / whole), CJK-aware token estimation. Protected blocks with per-type overflow
policy: oversized tables split at data-row boundaries with the header repeated in every
sub-chunk, oversized lists split at item boundaries. Single-pass heading merge produces
zero-fragment chunks carrying the deepest available `title_path`. Eligible chunks also
carry a `metadata.locator` pointing back to the original unit —
`{"kind":"page","start":3,"end":4}`, `{"kind":"slide","index":6}`,
`{"kind":"sheet","name":"売上集計"}`, `{"kind":"time","start_seconds":120,...}`. PDF
ranges are emitted only when every expected pagebreak survived chunking; otherwise
DocIngest warns and omits rather than guessing.

**Provenance.** Every chunk carries `metadata.lineage`: `source_markdown`,
`original_input` (filename / mimetype / binary hash / mtime), and an ordered
`transformations` array of what actually shaped it. Disabled features aren't recorded —
it's a positive provenance trail for citation and reproducibility, not a debug log.
`index.json` additionally exposes per-element PDF bounding boxes
(`files[].element_boxes`) for source highlighting, and under `vision_only` also
`files[].page_image_paths` so an image-RAG consumer can find each rendered page.

**Reliability.** Wall-clock timeouts on both parse and each Vision call, so one hung file
can't stall a run (the file is recorded with `error_type: "timeout"` and the pipeline
continues). Network-level retry with exponential backoff on every LLM call
(`models.defaults.max_retries`, default 2). Errors are classified (`timeout` /
`parse_error` / `chunk_error` / `io_error` / `interrupted` / `unknown`) so consumers can
branch without grepping messages. Ctrl+C between files finishes the current file, writes
everything completed so far, and exits 130 — a rerun resumes from cache. Multi-provider
fallback across Gemini / OpenAI / Anthropic / DashScope.

**Opt-in extras** (default OFF): PII sanitization (email / URL / credit card with Luhn /
IPv4 / JP phone — high-precision rules only, no name detection), repeating-furniture
dedup (collapses a per-page `DocuSign Envelope ID` to its first copy, never removing
every copy), exiftool metadata, contextual chunk summaries.

## Docs & tests

- **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** — phases, design rationale, extension guide
  (hooks / parsers / chunkers), known debt. §10 covers the graph layer.
- **[INTEGRATION.md](docs/INTEGRATION.md)** — embedding DocIngest into your own system
  (CLI subprocess / library / MCP), per-scenario recipes.
- **[PROCESSING_MODES.md](docs/PROCESSING_MODES.md)** — the mode × format knob matrix.
- **[COMPETITIVE_POSITIONING.md](docs/COMPETITIVE_POSITIONING.md)** — where this sits
  versus cloud suites and local tools, and which fights it deliberately skips.
- **[AGENTS.md](AGENTS.md)** — the command catalog for agents reading the repo directly.

```bash
python tests/unit/test_api.py               # public API: facade, outputs whitelist, providers
python tests/unit/test_lineage.py           # chunk lineage + transformations trail
python tests/unit/test_config_override.py   # config layering
python tests/incremental/run_tests.py       # incremental cache: modify / delete / config change
python tests/unit/test_graph_optional.py    # graph layer stays optional (with or without extras)
python tests/unit/test_graph_enrich.py      # chunks.jsonl preservation, idempotency
```

`tests/unit/test_mixed.py` runs the full parse path end to end and is slower than the
units above.
