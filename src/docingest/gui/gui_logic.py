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

    `options` carries the advanced-panel choices (chunk strategy, max pages,
    safety mode, markdown-only) already shaped as config_overrides /
    outputs. User settings are merged underneath the per-run options so a
    one-off choice on screen 02 wins over the saved default.
    """
    output = resolve_output_dir(library_name)

    overrides = dict(get_settings() or {})
    outputs = None
    if options:
        overrides.update(options.get("config_overrides") or {})
        outputs = options.get("outputs")

    result = api.ingest(
        list(paths),
        output=output,
        outputs=outputs,
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
    }


# ---------------------------------------------------------------------------
# Library list / summary / preview (04 + library picker)
# ---------------------------------------------------------------------------

def list_libraries() -> list[dict[str, Any]]:
    """Libraries under the GUI root, newest first."""
    return api.list_knowledge(library_root())


def library_summary(library_dir: str) -> dict[str, Any]:
    return api.get_summary(library_dir)


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


# ---------------------------------------------------------------------------
# Refine (04 → 10 dialog)
# ---------------------------------------------------------------------------

def refine(library_dir: str, files: list[str], skill: str) -> dict[str, Any]:
    """Produce a human-readable copy via refine(). `files` are sources/*.md
    names; `skill` is one of refine_faithful / refine_default / refine_html.
    Output is passed explicitly as the library dir (GUI owns the location),
    so refine writes readable/ under the library root."""
    src = Path(library_dir) / "sources"
    file_paths: list[str | Path] = [str(src / f) for f in files]
    results = api.refine(file_paths, skill=skill, output=library_dir)
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
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Build the knowledge graph for a library (calls an LLM per chunk).
    Reads chunks.jsonl; never touches originals. on_progress fires per chunk
    ({current,total,chunk_id,status}). User settings layer in as overrides so
    the graph LLM matches the configured models."""
    import docingest.graph as graph_api

    result = graph_api.build(
        library_dir,
        config_overrides=get_settings() or None,
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
    settings so cost-switch reporting matches the user's config."""
    config = api.build_config(config_overrides=get_settings() or None)
    return run_doctor(config)


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
    """Persist user settings; returns the written path as a string."""
    return str(save_settings(settings))
