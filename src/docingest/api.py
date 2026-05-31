"""
Public Python API — the "library face" of DocIngest.

Three top-level functions: ``ingest``, ``inspect``, ``refine``. Together with
the Provider classes (``docingest.providers``) and the ``IngestResult``
dataclass, this is the STABLE surface other projects should import. All
other modules (pipeline.py, parsers/, chunkers/, hooks/, output/, ...) are
internal and may change without notice.

Design principles
-----------------
* **Keyword-only signatures** — every function past the first positional
  argument is kw-only. Future parameters can be added without breaking
  existing callers.
* **Provider objects over env var** — callers inject credentials via
  ``vision=GeminiProvider(api_key=...)``; the underlying env-var / YAML
  paths still work unchanged for backwards compatibility.
* **Outputs whitelist** — ``outputs=["markdown", "chunks"]`` decides what
  the pipeline produces AND what gets read back into the result. ``None``
  means "produce everything the config says to produce" (legacy behaviour).
* **Returns the product, not just stats** — ``IngestResult`` carries the
  actual ``markdown_files`` / ``chunks`` / ``index`` / ``knowledge_map``
  content so callers don't have to re-read the output directory.
* **No new ceremony for config** — the existing four-layer merge
  (defaults < project yaml < env vars < overrides) is reused. Facade just
  accepts ``config_overrides`` in either flat dot-path form
  (``{"parsing.vision.max_pages": 100}``) or nested form
  (``{"parsing": {"vision": {"max_pages": 100}}}``) and feeds both into
  ``load_config(cli_overrides=...)``.

Round-trip semantics
--------------------
Pipeline still writes to disk — ``IngestResult`` is populated by reading
the produced files back. This keeps the pipeline's shape untouched (no
dual writer code paths) at the cost of one extra read pass. For typical
knowledge-base sizes the I/O is negligible; large-scale callers can set
``outputs=["chunks"]`` to skip reading markdown bulk back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Union

from .config import load_config, deep_merge, get_nested
from .providers import VisionProvider, AudioProvider, TextProvider


# ---------------------------------------------------------------------------
# Output whitelist
# ---------------------------------------------------------------------------
# Known output categories. Each maps to (a) a config key that controls
# whether the pipeline produces it, and (b) a reader used to load it back
# into the IngestResult. ``markdown`` and ``index`` are always produced
# when a file is processed successfully — their presence in the whitelist
# only toggles whether the reader runs on the way out.

_ALL_OUTPUTS: tuple[str, ...] = (
    "markdown",         # sources/*.md files
    "chunks",           # chunks.jsonl
    "index",            # index.json
    "knowledge_map",    # knowledge_map.yaml + knowledge_search.SKILL.md
    "quality_report",   # quality_report.json
    "run_log",          # log.md (append-only run history)
)

# When ``outputs`` is a proper subset of _ALL_OUTPUTS, we turn off the
# items NOT in the subset by pushing these config keys to False. Items
# with None don't need a config toggle (they're always produced) — the
# whitelist only controls whether the reader runs.
_OUTPUT_DISABLE_KEYS: dict[str, str | None] = {
    "markdown": None,                          # markdown is always written per file
    "chunks": "chunking.enabled",
    "index": None,                             # index.json is always written
    "knowledge_map": "knowledge_map.enabled",
    "quality_report": "quality_report.enabled",
    "run_log": "run_log.enabled",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class IngestResult:
    """
    Returned by :func:`ingest`.

    Which fields are populated depends on ``outputs``. Fields not requested
    stay at their default (empty list / empty dict / None) so callers can
    defensively check ``if result.chunks:`` without worrying about which
    outputs were enabled.
    """

    # Produced artefacts
    markdown_files: list[dict[str, Any]] = field(default_factory=list)
    """Each entry: ``{"path": "sources/a.md", "content": str, "metadata": dict}``."""

    chunks: list[dict[str, Any]] = field(default_factory=list)
    """Each entry: ``{"id": str, "text": str, "metadata": dict}``."""

    index: dict[str, Any] = field(default_factory=dict)
    """Content of ``index.json``."""

    knowledge_map: dict[str, Any] | None = None
    """Parsed ``knowledge_map.yaml`` content, or ``None`` when not produced."""

    quality_report: dict[str, Any] | None = None
    """Parsed ``quality_report.json``, or ``None`` when not produced."""

    # Run-level stats (a superset of the old PipelineResult headline fields)
    stats: dict[str, Any] = field(default_factory=dict)
    """
    Keys:
      total_files, successful, failed, total_chunks, total_tokens,
      elapsed_ms, errors, warnings, quality, token_usage, safety,
      interrupted.

    ``warnings`` and ``interrupted`` are forward-compatible additions —
    callers built against earlier shapes that don't check them stay
    correct (warnings defaults to [], interrupted to False).
    """

    output_dir: str = ""
    """Absolute path the pipeline wrote to (useful for later CLI ops)."""


# ---------------------------------------------------------------------------
# Config building — shared by ingest / inspect / refine and by MCP
# ---------------------------------------------------------------------------

# Public input alias used across the facade. A Provider argument may be
# either a full provider object OR a raw model_config dict (for advanced
# users who want to bypass the Provider class entirely).
ProviderArg = Union[VisionProvider, AudioProvider, TextProvider, dict, None]


def build_config(
    *,
    output: str | Path | None = None,
    outputs: list[str] | None = None,
    vision: ProviderArg = None,
    audio: ProviderArg = None,
    text: ProviderArg = None,
    config_overrides: dict[str, Any] | None = None,
    config_file: str | Path | None = None,
    force: bool | None = None,
    extra_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a fully merged config dict for a single facade call.

    All named parameters are optional; callers pass only what they want to
    override. Returns a dict ready to hand to ``run_pipeline`` / parsers /
    chunkers. Shared by ``ingest`` / ``inspect`` / ``refine`` and by
    ``mcp_server`` so the config-resolution rules live in exactly one place.

    Args:
        output: Output directory override (→ ``output.dir``).
        outputs: Whitelist of outputs to produce. See ``_ALL_OUTPUTS``.
        vision: Vision provider / raw model_config dict for
                ``models.vision``.
        audio: Audio provider / raw model_config dict for
                ``models.audio_transcription``.
        text: Text provider / raw model_config dict. Uses the provider's
                ``.task`` attribute to pick the config section
                (default: "chunking_assist").
        config_overrides: Free-form overrides. Accepts either nested-dict
                form (``{"parsing": {"vision": {...}}}``) or flat
                dot-path form (``{"parsing.vision.max_pages": 100}``) or
                a mix. Keys containing '.' are treated as dot-paths.
        config_file: Path to a project-level ``docingest.yaml``. When
                None, the normal auto-discovery logic in load_config
                applies (picks up ``docingest.yaml`` in cwd if present).
        force: Force-rebuild flag (→ ``incremental.force``). ``None``
                means "don't touch the setting".
        extra_overrides: Already-merged override dict to apply LAST (after
                everything else). Useful for CLI-style adapters that
                want to guarantee their args win.

    Returns:
        Merged config dict with all precedence layers resolved.
    """
    layered: dict[str, Any] = {}

    if output is not None:
        _set_dotted(layered, "output.dir", str(output))

    if outputs is not None:
        _apply_output_whitelist(layered, outputs)

    if vision is not None:
        _merge_provider(layered, "models.vision", vision)

    if audio is not None:
        _merge_provider(layered, "models.audio_transcription", audio)

    if text is not None:
        task = getattr(text, "task", "chunking_assist") if not isinstance(text, dict) else "chunking_assist"
        _merge_provider(layered, f"models.{task}", text)

    if force is True:
        _set_dotted(layered, "incremental.force", True)

    if config_overrides:
        user = _normalize_overrides(config_overrides)
        layered = deep_merge(layered, user)

    if extra_overrides:
        layered = deep_merge(layered, extra_overrides)

    return load_config(
        project_config_path=Path(config_file) if config_file else None,
        cli_overrides=layered,
    )


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

def ingest(
    paths: str | Path | list[str | Path],
    *,
    output: str | Path | None = None,
    outputs: list[str] | None = None,
    vision: ProviderArg = None,
    audio: ProviderArg = None,
    text: ProviderArg = None,
    config_overrides: dict[str, Any] | None = None,
    config_file: str | Path | None = None,
    force: bool = False,
    acknowledge_large: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    install_signal_handler: bool = False,
) -> IngestResult:
    """
    Process documents into a knowledge base (library entry point).

    Args:
        paths: A single path/URL or a list of them. Directories are
            expanded recursively by the pipeline.
        output: Base output directory. When None, derived from ``paths``:
            single input → ``./knowledge/<stem>/``; multiple inputs → the
            current working directory's ``./knowledge/`` (pipeline will
            refuse to auto-name in that case, matching CLI behaviour).
        outputs: Whitelist of outputs to produce / read back. Elements
            must come from: ``"markdown"``, ``"chunks"``, ``"index"``,
            ``"knowledge_map"``, ``"quality_report"``, ``"run_log"``.
            ``None`` (default) means "everything the config enables",
            preserving legacy behaviour.
        vision: Vision provider. Typically one of the provider classes
            from ``docingest.providers`` (e.g. ``GeminiProvider(api_key=...)``).
            ``None`` keeps the YAML/env configuration unchanged.
        audio: Audio transcription provider.
        text: Text-completion provider (affects knowledge map AI summary,
            refine, chunking assist).
        config_overrides: Free-form config overrides (nested dict OR flat
            dot-path dict OR a mix of both). Applied on top of layered
            defaults.
        config_file: Path to a project-level ``docingest.yaml``.
        force: Ignore incremental cache and reprocess all files.
        acknowledge_large: When ``safety.mode`` is ``"strict"`` and the
            pre-run check flags violations, set this to proceed anyway.
        on_progress: Optional callback fired once per file completion
            (cached, processed, failed, or skipped due to a graceful
            interrupt). Receives a single dict — see
            :func:`docingest.pipeline.run_pipeline` for the event schema.
            Useful for piping progress to a UI / SSE stream.
        install_signal_handler: When True, the pipeline installs its
            graceful-stop SIGINT handler for the duration of the run.
            Default False so library callers (web servers, long-running
            hosts) keep their own signal handling intact. The CLI passes
            True to give users the expected Ctrl+C-stops-cleanly behaviour.

    Returns:
        :class:`IngestResult` — statistics + actual artefact contents
        filtered by the ``outputs`` whitelist.
    """
    from .pipeline import run_pipeline
    from .parsers import create_parser
    from .chunkers import create_chunker

    # Normalize paths to a list. URLs (strings starting with http/https)
    # are kept as-is and not wrapped in Path — pipeline.discover_files
    # detects them by string prefix.
    path_list = _normalize_paths(paths)

    # Resolve output dir consistently with CLI: single input → auto-stem,
    # multi-input → require explicit (we pick cwd/knowledge as a reasonable
    # library default; pipeline will still refuse mixed-input auto-deriving
    # inside discover_files → output conflicts are caught at write time).
    if output is None:
        if len(path_list) == 1 and not _is_url(path_list[0]):
            first = Path(path_list[0])
            output = Path("./knowledge") / first.stem
        else:
            output = Path("./knowledge")

    config = build_config(
        output=output,
        outputs=outputs,
        vision=vision,
        audio=audio,
        text=text,
        config_overrides=config_overrides,
        config_file=config_file,
        force=force,
    )

    parser = create_parser(config)
    chunker = create_chunker(config) if get_nested(config, "chunking.enabled", True) else None

    # Run the pipeline — strings (URLs) and Paths both pass straight
    # through discover_files which handles the distinction.
    pipeline_input: list[Path | str] = [
        p if _is_url(p) else Path(p) for p in path_list
    ]
    pipeline_result = run_pipeline(
        input_paths=pipeline_input,
        config=config,
        parser=parser,
        chunker=chunker,
        acknowledge_large=acknowledge_large,
        on_progress=on_progress,
        install_signal_handler=install_signal_handler,
    )

    # Build IngestResult by reading artefacts back from disk, filtered by
    # the outputs whitelist. Safety-aborted runs skip reading (nothing to
    # read) but still return a populated `stats` so callers can inspect.
    output_dir_path = Path(get_nested(config, "output.dir", "./knowledge")).resolve()
    result = IngestResult(
        output_dir=str(output_dir_path),
        stats=_pipeline_stats(pipeline_result),
    )

    if pipeline_result.safety.get("aborted"):
        return result

    wanted = _resolve_wanted(outputs)
    _populate_artefacts(result, output_dir_path, config, wanted)

    # Write meta.json so the library list has a friendly name + provenance.
    # Best-effort: a meta write failure must never fail a successful ingest.
    try:
        from .utils.library import write_library_meta
        write_library_meta(
            output_dir_path,
            source_files=[str(p) for p in path_list],
        )
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# inspect / refine
# ---------------------------------------------------------------------------

def inspect(
    paths: str | Path | list[str | Path],
    *,
    config_overrides: dict[str, Any] | None = None,
    config_file: str | Path | None = None,
) -> list[dict[str, Any]]:
    """
    Pre-flight inspection — fast size/pages/cost report without parsing.

    Thin wrapper around :func:`docingest.inspect.inspect_files` that
    routes through :func:`build_config` so callers can pass
    ``config_overrides`` in the same form ``ingest`` accepts.
    """
    from .inspect import inspect_files

    config = build_config(
        config_overrides=config_overrides,
        config_file=config_file,
    )
    path_list = _normalize_paths(paths)
    return inspect_files(
        [p if _is_url(p) else Path(p) for p in path_list],
        config,
    )


def refine(
    files: str | Path | list[str | Path],
    *,
    output: str | Path | None = None,
    skill: str | None = None,
    text: ProviderArg = None,
    config_overrides: dict[str, Any] | None = None,
    config_file: str | Path | None = None,
) -> list[dict[str, Any]]:
    """
    Refine sources/*.md into human-readable form via an LLM.

    Same routing behaviour as the CLI: when ``output`` is None and the
    first file lives in a ``sources/`` directory, the knowledge-base root
    is used; otherwise the first file's parent directory.
    """
    from .refine import refine_files

    file_list = _normalize_paths(files)
    file_paths = [Path(f) for f in file_list]

    if output is None:
        first = file_paths[0].resolve()
        output = first.parent.parent if first.parent.name == "sources" else first.parent

    config = build_config(
        text=text,
        config_overrides=config_overrides,
        config_file=config_file,
    )
    return refine_files(file_paths, config, Path(output), skill)


# ---------------------------------------------------------------------------
# Library management (data-layer; used by GUI / CLI / future web agent)
# ---------------------------------------------------------------------------

def list_knowledge(
    root: str | Path | None = None,
    *,
    config_file: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List processed knowledge libraries under ``root`` (default ./knowledge).

    Each entry: ``{name, dir, display_name, files, chunks, created_at,
    has_meta}``. ``has_meta`` lets a caller show only GUI-created libraries.
    The index filename is read from config (``output.index_file``) so a
    renamed index is still recognized. Tolerant — skips non-library /
    unreadable dirs, never raises.
    """
    from .config import load_config
    from .utils.library import list_libraries
    cfg = load_config(project_config_path=config_file)
    return list_libraries(
        root,
        index_name=get_nested(cfg, "output.index_file", "index.json"),
    )


def get_summary(
    library_dir: str | Path,
    *,
    config_file: str | Path | None = None,
) -> dict[str, Any]:
    """Summary of one library (index + quality report) for a done screen /
    library detail. ``{dir, exists, display_name, stats, files, quality}``;
    ``exists=False`` when the dir isn't a library. Index / quality filenames
    are read from config (``output.index_file`` / ``quality.output_file``)."""
    from .config import load_config
    from .utils.library import library_summary
    cfg = load_config(project_config_path=config_file)
    return library_summary(
        library_dir,
        index_name=get_nested(cfg, "output.index_file", "index.json"),
        quality_name=get_nested(cfg, "quality.output_file", "quality_report.json"),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_paths(paths: str | Path | list[str | Path]) -> list[str | Path]:
    """Always return a list; preserve strings so URL detection still works."""
    if isinstance(paths, (str, Path)):
        return [paths]
    return list(paths)


def _is_url(value: str | Path) -> bool:
    """Best-effort URL detection matching discover_files' own check."""
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return lowered.startswith(("http://", "https://"))


def _set_dotted(target: dict[str, Any], dotted_path: str, value: Any) -> None:
    """Set ``target[a][b][c] = value`` for dotted_path "a.b.c"."""
    keys = dotted_path.split(".")
    cur = target
    for key in keys[:-1]:
        existing = cur.get(key)
        if not isinstance(existing, dict):
            cur[key] = {}
        cur = cur[key]
    cur[keys[-1]] = value


def _normalize_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Accept either nested or flat dot-path overrides (or a mix).

    Every top-level key containing '.' is treated as a dot-path and
    expanded into nested form; plain keys are taken as-is and deep-merged.
    This lets callers write the compact form without losing the
    explicitness of the nested form when they want it.
    """
    nested: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(key, str) and "." in key:
            _set_dotted(nested, key, value)
        else:
            nested = deep_merge(nested, {key: value})
    return nested


def _apply_output_whitelist(layered: dict[str, Any], outputs: list[str]) -> None:
    """
    Translate the outputs whitelist into config toggles (deep-merged into
    ``layered``). Unknown output names raise ValueError — fails fast
    rather than silently ignoring typos that would cost users API $$.
    """
    unknown = set(outputs) - set(_ALL_OUTPUTS)
    if unknown:
        raise ValueError(
            f"Unknown output(s): {sorted(unknown)}. "
            f"Valid options: {list(_ALL_OUTPUTS)}"
        )
    wanted = set(outputs)
    for name, cfg_path in _OUTPUT_DISABLE_KEYS.items():
        if cfg_path is None:
            continue
        if name not in wanted:
            _set_dotted(layered, cfg_path, False)


def _merge_provider(
    layered: dict[str, Any],
    cfg_section: str,
    value: ProviderArg,
) -> None:
    """
    Merge a Provider object OR a raw model_config dict into config at cfg_section.

    The runtime type check catches callers who bypass the static type hint
    (e.g. passing a tuple from an old API). Pyright sees the static union
    as exhaustive after the isinstance branches; the final raise exists
    only for runtime safety.
    """
    if value is None:
        return
    if isinstance(value, (VisionProvider, AudioProvider, TextProvider)):
        payload = value.to_model_config()
    elif isinstance(value, dict):
        payload = value
    else:  # type: ignore[unreachable]
        raise TypeError(  # type: ignore[unreachable]
            f"{cfg_section} expects a Provider / dict / None, got {type(value).__name__}"
        )
    # Build {"models": {<section_tail>: payload}} and deep-merge in place.
    # cfg_section is always "models.vision" / "models.audio_transcription" /
    # "models.chunking_assist" — two segments.
    parts = cfg_section.split(".")
    cursor: dict[str, Any] = layered
    for segment in parts[:-1]:
        cursor = cursor.setdefault(segment, {})
    tail = parts[-1]
    cursor[tail] = deep_merge(cursor.get(tail, {}), payload)


def _resolve_wanted(outputs: list[str] | None) -> set[str]:
    """
    Determine which artefact readers should run. None → read everything
    the pipeline would have produced (preserves the old "no whitelist"
    convenience); a list → validated whitelist.
    """
    if outputs is None:
        return set(_ALL_OUTPUTS)
    unknown = set(outputs) - set(_ALL_OUTPUTS)
    if unknown:
        raise ValueError(
            f"Unknown output(s): {sorted(unknown)}. "
            f"Valid options: {list(_ALL_OUTPUTS)}"
        )
    return set(outputs)


def _pipeline_stats(pipeline_result: Any) -> dict[str, Any]:
    """Flatten the PipelineResult into a plain dict for IngestResult.stats."""
    return {
        "total_files": pipeline_result.total_files,
        "successful": pipeline_result.successful,
        "failed": pipeline_result.failed,
        "total_chunks": pipeline_result.total_chunks,
        "total_tokens": pipeline_result.total_tokens,
        "elapsed_ms": pipeline_result.elapsed_ms,
        "errors": list(pipeline_result.errors),
        # Non-fatal per-run warnings — files processed successfully but with a
        # quality compromise (page cap hit, OCR engine downgraded, ...). The
        # invariant `successful == N AND not warnings` means everything ran
        # cleanly. Each entry: {"file": str, "message": str}.
        # getattr with default keeps old call sites (that built PipelineResult
        # manually without this field) backwards-compatible.
        "warnings": list(getattr(pipeline_result, "warnings", []) or []),
        "quality": dict(pipeline_result.quality),
        "token_usage": dict(pipeline_result.token_usage),
        "safety": dict(pipeline_result.safety),
        "interrupted": bool(getattr(pipeline_result, "interrupted", False)),
    }


def _populate_artefacts(
    result: IngestResult,
    output_dir: Path,
    config: dict[str, Any],
    wanted: set[str],
) -> None:
    """
    Read the pipeline's on-disk artefacts back into the IngestResult,
    honouring the ``wanted`` whitelist. Missing files simply leave the
    corresponding field at its default — reading an absent artefact is
    not an error (the pipeline may have disabled it via config).
    """
    sources_dir_name = get_nested(config, "output.sources_dir", "sources")
    sources_dir = output_dir / sources_dir_name

    if "markdown" in wanted and sources_dir.exists():
        for md_path in sorted(sources_dir.glob("*.md")):
            try:
                content = md_path.read_text(encoding="utf-8")
            except OSError:
                continue
            result.markdown_files.append({
                "path": str(md_path.relative_to(output_dir)).replace("\\", "/"),
                "content": content,
                "metadata": _parse_frontmatter(content),
            })

    if "chunks" in wanted:
        chunks_file = output_dir / get_nested(config, "chunking.output_file", "chunks.jsonl")
        if chunks_file.exists():
            result.chunks = _read_jsonl(chunks_file)

    if "index" in wanted:
        index_file = output_dir / get_nested(config, "output.index_file", "index.json")
        if index_file.exists():
            try:
                result.index = json.loads(index_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

    if "knowledge_map" in wanted:
        # Filename is fixed in output/knowledge_map.py — no config key.
        km_file = output_dir / "knowledge_map.yaml"
        if km_file.exists():
            try:
                import yaml
                loaded = yaml.safe_load(km_file.read_text(encoding="utf-8"))
                result.knowledge_map = loaded if isinstance(loaded, dict) else None
            except Exception:
                pass

    if "quality_report" in wanted:
        qr_name = get_nested(config, "quality_report.output_file", "quality_report.json")
        qr_file = output_dir / qr_name
        if qr_file.exists():
            try:
                result.quality_report = json.loads(qr_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dicts. Malformed lines are skipped."""
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    records.append(json.loads(stripped))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return records


def _parse_frontmatter(markdown: str) -> dict[str, Any]:
    """
    Return the YAML frontmatter block of ``markdown`` as a dict.

    Empty dict when there's no frontmatter or it fails to parse. Mirrors
    the same heuristic pipeline._parse_frontmatter uses so the metadata
    shape returned here matches what index.json records.
    """
    if not markdown.startswith("---\n"):
        return {}
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return {}
    try:
        import yaml
        data = yaml.safe_load(markdown[4:end])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


__all__ = [
    "ingest",
    "inspect",
    "refine",
    "list_knowledge",
    "get_summary",
    "build_config",
    "IngestResult",
]
