"""
Adapter layer — translates GUI intents into docingest.api calls and shapes
results into plain dicts the frontend can render.

Decoupling rule (GUI_DESIGN.md): no UI-framework type appears in any
signature here. Progress is delivered via a plain ``on_progress(event: dict)``
callback; results are dicts / lists of dicts. This layer knows nothing about
pywebview — so the same logic serves any future shell (PyQt, web, ...).

The core (docingest.api) is never modified; this is a thin caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

from .. import api
from ..config import get_settings, save_settings
from ..doctor import run_doctor
from ..utils.library import default_library_root


# ---------------------------------------------------------------------------
# Output location — GUI fixes libraries under a stable, writable user root
# (BACKEND_API §3.0). The api default (./knowledge) is NOT changed; the GUI
# adapter simply passes an explicit absolute `output` every time.
# ---------------------------------------------------------------------------

def library_root() -> Path:
    """Absolute root where GUI-created libraries live. Created on demand."""
    root = default_library_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slugify(name: str) -> str:
    """Friendly, filesystem-safe dir name from a user-given library name.
    Keeps it human-recognizable (BACKEND_API §3.2: dir name = friendly name,
    not a uuid). Falls back to 'library' when nothing usable remains."""
    cleaned = "".join(c if c.isalnum() or c in (" ", "-", "_") else "-" for c in name)
    cleaned = "-".join(cleaned.split()).strip("-_")
    return cleaned or "library"


def resolve_output_dir(library_name: str) -> Path:
    """Pick the absolute output dir for a new ingest. GUI owns this choice so
    runs never collide and a packaged exe doesn't drift with cwd."""
    return library_root() / _slugify(library_name)


# ---------------------------------------------------------------------------
# Pre-flight inspection (01/02 + 09 cost dialog)
# ---------------------------------------------------------------------------

def inspect_paths(paths: list[str | Path]) -> dict[str, Any]:
    """Pre-flight for the cost dialog (09). Returns:

        {
          "files": [<inspect row> + "violations": [{metric,value,threshold}], ...],
          "totals": {"pages": int, "est_cost_usd": float},
          "run_violations": [{metric,value,threshold}, ...],
        }

    inspect() alone only gives a `recommendation` string per file; the dialog
    needs structured over-budget info (which file, which dimension, the actual
    threshold) to render «128 ページ / 上限 100» precisely. We get that by
    reusing safety.check_file_violations / check_run_violations against the
    SAME merged config inspect used — pure computation, no LLM, no backend
    change. User settings layer in so the thresholds match a real run.
    """
    from ..safety import check_file_violations, check_run_violations

    overrides = get_settings() or None
    config = api.build_config(config_overrides=overrides)
    rows = api.inspect(paths, config_overrides=overrides)

    for row in rows:
        row["violations"] = check_file_violations(row, config)

    totals = {
        "pages": sum((r.get("pages") or 0) for r in rows),
        "est_cost_usd": round(sum((r.get("est_cost_usd") or 0.0) for r in rows), 4),
    }
    return {
        "files": rows,
        "totals": totals,
        "run_violations": check_run_violations(rows, config),
    }


# ---------------------------------------------------------------------------
# Artifact inventory — what was ACTUALLY produced on disk (04 done screen)
# ---------------------------------------------------------------------------
# The done screen lists "生成物". Which artefacts exist depends on the run's
# `purpose` / `outputs` (e.g. "Markdown のみ" produces NO chunks.jsonl). Hard-
# coding the four-item list in the frontend would make the UI lie when an
# artefact wasn't produced. So we scan the output dir and report what's really
# there — pure existence checks, no LLM, cheap. (UI-不撒谎: frontend-design §二)

# Each artefact: (key, probe). probe(dir) -> count|bool|None. The frontend maps
# key → label/icon; a falsy/None probe means "not produced" → don't render it.
def _scan_artifacts(output_dir: "str | Path") -> list[dict[str, Any]]:
    """Inventory the real artefacts under a library dir, in display order.
    Returns ``[{key, present, count}]`` — count is a file/chunk tally where
    meaningful (sources md count, chunks count from index), else None. Never
    raises: a missing/unreadable dir yields an empty-ish list, the caller
    renders whatever is present."""
    d = Path(output_dir)
    items: list[dict[str, Any]] = []

    # sources/: count the .md files actually written.
    src = d / "sources"
    md_count = 0
    if src.is_dir():
        try:
            md_count = sum(1 for p in src.iterdir() if p.suffix.lower() == ".md")
        except OSError:
            md_count = 0
    items.append({"key": "sources", "present": md_count > 0, "count": md_count})

    # chunks.jsonl: present only when chunking ran. Count from index stats so
    # we don't re-read the jsonl (the index already aggregates total_chunks).
    chunks_file = d / "chunks.jsonl"
    chunk_count = None
    if chunks_file.is_file():
        idx = _read_index(d)
        chunk_count = (idx.get("stats", {}) or {}).get("total_chunks")
    items.append({
        "key": "chunks", "present": chunks_file.is_file(), "count": chunk_count,
    })

    # index.json: always written by a successful run (cache + discovery need it).
    items.append({
        "key": "index", "present": (d / "index.json").is_file(), "count": None,
    })

    # knowledge_map.yaml: only when the knowledge_map stage ran.
    items.append({
        "key": "knowledge_map",
        "present": (d / "knowledge_map.yaml").is_file(),
        "count": None,
    })

    # graph/: optional GraphRAG layer (built separately). Surface it when built
    # so the done screen reflects reality, not just the ingest-time artefacts.
    items.append({
        "key": "graph", "present": (d / "graph").is_dir(), "count": None,
    })

    # Only return artefacts that actually exist — the frontend renders the list
    # verbatim, so filtering here keeps "UI 不撒谎" the single source of truth.
    return [it for it in items if it["present"]]


def _read_index(d: Path) -> dict[str, Any]:
    """Best-effort read of a library's index.json (for chunk counts). Empty
    dict on any failure — callers treat a missing count as 'unknown'."""
    try:
        import json
        return json.loads((d / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# Artefact key → on-disk path, relative to the library dir. Used when the user
# clicks an artefact row to "really open" it (sources is previewed in-window;
# the machine-readable ones open in the OS default app — they're meant for
# code editors, not a GUI viewer). None means "no single file" (sources is a
# dir; the frontend previews it in-window instead of opening it here).
_ARTIFACT_PATHS = {
    "sources": "sources",            # a dir — opened only as a fallback
    "chunks": "chunks.jsonl",
    "index": "index.json",
    "knowledge_map": "knowledge_map.yaml",
    "graph": "graph",                # a dir
    "run_log": "run.log",            # ingest log (always written by the pipeline)
}


def artifact_path(library_dir: str, key: str) -> str:
    """Resolve an artefact key to its absolute on-disk path within a library,
    or "" when the key is unknown or the file/dir doesn't exist. The path is
    confined to the library dir (key maps to a fixed relative path — no user
    string is joined in, so there's no traversal surface, but we still verify
    existence so the caller never shells open a missing path)."""
    rel = _ARTIFACT_PATHS.get(key)
    if not rel:
        return ""
    target = (Path(library_dir) / rel).resolve()
    return str(target) if target.exists() else ""


# ---------------------------------------------------------------------------
# Ingest (03 progress → 04 done)
# ---------------------------------------------------------------------------

def run_ingest(
    paths: Sequence[str | Path],
    library_name: str,
    *,
    options: dict[str, Any] | None = None,
    acknowledge_large: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Process documents into a library and return a summary dict for the
    done screen.

    `options` carries the advanced-panel choices (chunk strategy, safety
    mode, output purpose) already shaped as config_overrides / outputs /
    purpose. User settings are merged underneath the per-run options so a
    one-off choice on screen 02 wins over the saved default.
    """
    output = resolve_output_dir(library_name)

    overrides = dict(get_settings() or {})
    outputs = None
    purpose = None
    if options:
        overrides.update(options.get("config_overrides") or {})
        outputs = options.get("outputs")
        purpose = options.get("purpose")

    result = api.ingest(
        list(paths),
        output=output,
        outputs=outputs,
        purpose=purpose,
        config_overrides=overrides or None,
        acknowledge_large=acknowledge_large,
        on_progress=on_progress,
    )

    # Persist the user-facing name so the library list shows it (the default
    # meta.json written by ingest uses the dir name).
    try:
        from ..utils.library import write_library_meta
        write_library_meta(
            output, source_files=[str(p) for p in paths], display_name=library_name
        )
    except Exception:
        pass

    return {
        "output_dir": result.output_dir,
        "stats": result.stats,
        "summary": api.get_summary(output),
        # Real artefacts on disk (depends on purpose/outputs) — see _scan_artifacts.
        "artifacts": _scan_artifacts(output),
    }


# ---------------------------------------------------------------------------
# Library list / summary / preview (04 + library picker)
# ---------------------------------------------------------------------------

def list_libraries() -> list[dict[str, Any]]:
    """Libraries under the GUI root, newest first."""
    return api.list_knowledge(library_root())


def library_summary(library_dir: str) -> dict[str, Any]:
    """Summary for opening an EXISTING library (history). Adds the same real
    artefact inventory the done screen uses after a fresh ingest, so an opened
    library lists what's actually on disk (not a hardcoded set)."""
    summary = api.get_summary(library_dir)
    summary["artifacts"] = _scan_artifacts(library_dir)
    return summary


def preview_markdown(library_dir: str, filename: str) -> str:
    """Read one ``sources/<filename>`` for the preview pane. The backend has
    no preview_markdown() yet (BACKEND_API listed it as to-do); reading the
    clean Markdown product directly here is the minimal, decoupled approach —
    the file IS the product, no transformation needed. Returns "" if absent.

    Resolves the path under the library's sources/ and refuses to escape it
    (filename is user-selectable input → validate at this boundary)."""
    base = (Path(library_dir) / "sources").resolve()
    target = (base / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return ""
    if not target.is_file():
        return ""
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return ""


def list_refined(library_dir: str, source_filename: str) -> list[dict[str, str]]:
    """Find refined copies of one source file. refine writes to
    readable/<skill_short>/<name>, where skill_short is the skill minus its
    "refine_" prefix and html skills emit a .html sibling (refine.py). So for a
    source "spec.md" we look across all readable/<skill_short>/ dirs for either
    "spec.md" or "spec.html". Returns [{skill, filename, path}] (skill is the
    short name, e.g. "faithful"); empty when none exist. Pure, read-only,
    best-effort: the preview "整形版" toggle is only enabled when this is
    non-empty."""
    readable = (Path(library_dir) / "readable").resolve()
    if not readable.is_dir():
        return []
    stem = Path(source_filename).stem
    out: list[dict[str, str]] = []
    try:
        for skill_dir in sorted(readable.iterdir()):
            if not skill_dir.is_dir():
                continue
            for cand in (stem + ".md", stem + ".html"):
                f = skill_dir / cand
                if f.is_file():
                    out.append({
                        "skill": skill_dir.name,
                        "filename": cand,
                        "path": str(f.resolve()),
                    })
    except OSError:
        return []
    return out


def preview_refined(library_dir: str, skill: str, filename: str) -> str:
    """Read one refined file (readable/<skill>/<filename>) for the preview
    pane's "整形版" view. Same boundary discipline as preview_markdown: confine
    the resolved path under readable/ and refuse to escape it (skill + filename
    are frontend-supplied -> validate at this boundary). Returns "" if absent.
    HTML refine output is returned as-is (the frontend decides how to render)."""
    base = (Path(library_dir) / "readable").resolve()
    target = (base / skill / filename).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return ""
    if not target.is_file():
        return ""
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Refine (04 -> 10 dialog)
# ---------------------------------------------------------------------------

def refine(
    library_dir: str,
    files: list[str],
    skill: str,
    acknowledge: bool = False,
) -> dict[str, Any]:
    """Produce a human-readable copy via refine(). `files` are sources/*.md
    names; `skill` is one of refine_faithful / refine_default / refine_html.
    Output is passed explicitly as the library dir (GUI owns the location),
    so refine writes readable/ under the library root.

    Large files are split + refined in parallel by the core. When the cost
    gate (refine.cost_check.mode=strict) trips, api.refine returns a single
    {"blocked": True, "estimate": ...} result; we surface that as
    {"blocked": True, ...} so the UI can confirm and re-call with
    acknowledge=True. User settings layer in so the gate matches config."""
    src = Path(library_dir) / "sources"
    file_paths: list[str | Path] = [str(src / f) for f in files]
    results = api.refine(
        file_paths,
        skill=skill,
        output=library_dir,
        config_overrides=get_settings() or None,
        acknowledge=acknowledge,
    )
    # Cost gate blocked the run (strict mode, over budget, not acknowledged).
    if len(results) == 1 and isinstance(results[0], dict) and results[0].get("blocked"):
        return {"blocked": True, **results[0]}
    return {"files": results}


# ---------------------------------------------------------------------------
# Knowledge graph (11 未構築 / 12 構築中) — optional, opt-in layer
# ---------------------------------------------------------------------------
# The graph layer is an optional extra (lightrag). We import docingest.graph
# lazily so the GUI doesn't load lightrag unless the user opens a graph screen,
# and so a missing install surfaces as a clear message rather than an import
# crash at module load.

def graph_status(library_dir: str) -> dict[str, Any]:
    """Whether a knowledge graph is built for this library + counts. Returns
    {built, entities, relations, communities, available}. `available=False`
    when the graph extra isn't installed (the screen then explains that)."""
    try:
        import docingest.graph as graph_api
    except Exception:
        return {"available": False, "built": False}
    try:
        st = graph_api.status(library_dir)
        return {
            "available": True,
            "built": st.built,
            "entities": st.entities_count,
            "relations": st.relations_count,
            "communities": st.communities_count,
        }
    except Exception as e:
        # Status is pure inspection; a failure means "can't tell" → not built.
        return {"available": True, "built": False, "error": str(e)}


def build_graph(
    library_dir: str,
    *,
    mode: str | None = None,
    enrich_chunks: bool = False,
    force: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Build the knowledge graph for a library (calls an LLM per chunk).
    Reads chunks.jsonl; never touches originals. on_progress fires per chunk
    ({current,total,chunk_id,status}). User settings layer in as overrides so
    the graph LLM matches the configured models.

    Per-run knobs (all default off → exactly the prior behaviour):
      mode           — "vector_only" (cheap) or "full" (default in config).
                       None = use the resolved config default.
      enrich_chunks  — also write chunks_enriched.jsonl in the same call
                       (CLI's --enrich-chunks). Injected via config override
                       so it wins over a saved-settings default.
      force          — ignore the per-chunk extraction cache.
    """
    import docingest.graph as graph_api

    # Start from saved user settings; layer per-run choices on top so a one-off
    # choice on the build screen wins over the saved default (parallels how
    # run_ingest merges 02-screen options).
    overrides: dict[str, Any] = dict(get_settings() or {})
    # The build screen's enrich switch is the authoritative intent for THIS run.
    # Always write it (not just when True) so a saved-settings default can't
    # silently flip the behaviour behind the user's back — UI tells the truth.
    overrides["graph.enrich_chunks.enabled"] = bool(enrich_chunks)

    result = graph_api.build(
        library_dir,
        mode=mode,
        force=force,
        config_overrides=overrides or None,
        on_progress=on_progress,
    )
    return {
        "entities": result.entities_count,
        "relations": result.relations_count,
        "communities": result.communities_count,
        "chunks_processed": result.chunks_processed,
        "elapsed_ms": result.elapsed_ms,
        "errors": list(result.errors),
    }


# ---------------------------------------------------------------------------
# Environment check / settings (05 / 06 / 07)
# ---------------------------------------------------------------------------

def doctor() -> dict[str, Any]:
    """Environment check (API keys + external tools), layered with saved
    settings so cost-switch reporting matches the user's config.

    fast=True: the env-check screen only renders 「検出済み / 未設定」 (no
    version strings), so we skip the heavy __import__ path — drops the call
    from ~9s to ~10ms on a full install (sentence_transformers alone costs
    ~5s when imported just to read its version)."""
    config = api.build_config(config_overrides=get_settings() or None)
    return run_doctor(config, fast=True)


def load_settings() -> dict[str, Any]:
    return get_settings()


def effective_safety() -> dict[str, Any]:
    """Current effective safety thresholds (cost-limit screen needs real
    initial values, not hardcoded ones). Merges saved user settings over the
    config defaults and returns the resolved `safety` subset. So the screen
    shows what a real run would actually use — and if the YAML defaults change,
    the screen follows, no front-end duplication."""
    from ..config import get_nested

    config = api.build_config(config_overrides=get_settings() or None)
    return {
        "mode": get_nested(config, "safety.mode", "strict"),
        "per_file": {
            "max_pages": get_nested(config, "safety.per_file.max_pages"),
            "max_est_cost_usd": get_nested(config, "safety.per_file.max_est_cost_usd"),
        },
        "per_run": {
            "max_total_pages": get_nested(config, "safety.per_run.max_total_pages"),
            "max_total_est_cost_usd": get_nested(config, "safety.per_run.max_total_est_cost_usd"),
        },
    }


def store_settings(settings: dict[str, Any]) -> str:
    """Persist user settings; returns the written path as a string.
    Also mirrors any *_API_KEY entries into os.environ so the env-check
    screen — and downstream providers (litellm / genai), which all read
    from environ — pick them up in the same process without restart."""
    path = str(save_settings(settings))
    _sync_api_keys_to_environ(settings)
    return path


# Keys treated as API credentials when mirroring settings → environ. Mirrors
# the list the env-check screen displays (doctor.py).
_API_KEY_NAMES = ("GEMINI_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY")


def _sync_api_keys_to_environ(settings: dict[str, Any]) -> None:
    """Copy any API key found in user settings into os.environ.
    Single source of truth = environ (matches provider.py's policy: it
    sets env vars then lets litellm/genai read them). Empty / missing
    values do NOT clear environ — that would silently wipe a .env-supplied
    key when the user opens settings without touching the field."""
    import os
    for name in _API_KEY_NAMES:
        value = settings.get(name)
        if value:
            os.environ[name] = str(value)


def hydrate_environ_from_settings() -> None:
    """Called once at GUI startup. Reads the saved user settings and pushes
    any *_API_KEY into os.environ, so a key the user previously entered in
    the GUI is visible to doctor() / providers from the first action.
    .env (load_dotenv) still runs separately at process start; this is the
    GUI-saved-settings counterpart, not a replacement."""
    _sync_api_keys_to_environ(get_settings() or {})
