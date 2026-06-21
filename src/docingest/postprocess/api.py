"""
Public API for the optional post-processing layer.

One top-level operation today: ``run`` (template-driven extraction). The
shape echoes docingest.graph.api so callers familiar with the graph facade
hit no surprise:

  * Keyword-only past the first positional arg.
  * Provider / config injection via the same four-layer load_config merge.
  * Backend-agnostic: ``processor=`` selects the post-processor; extract is
    the only one today.

This layer NEVER initialises implicitly — ``import docingest`` does not load
it. Users explicitly ``import docingest.postprocess`` (or use the
``docingest extract`` CLI).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Union

from ..config import load_config, deep_merge, get_nested
from ..providers import VisionProvider, AudioProvider, TextProvider
from .base import Runner, RunResult


ProviderArg = Union[VisionProvider, AudioProvider, TextProvider, dict, None]


@dataclass
class ExtractResult:
    """Returned by :func:`run`. Wraps the Runner's RunResult + output path."""

    processor: str = ""
    template: str = ""
    units_total: int = 0
    units_ok: int = 0
    units_failed: int = 0
    pieces_total: int = 0
    pieces_failed: int = 0
    elapsed_ms: int = 0
    output_path: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _normalize_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept nested OR flat dot-path overrides (mirrors docingest.api)."""
    nested: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(key, str) and "." in key:
            cur = nested
            parts = key.split(".")
            for p in parts[:-1]:
                if not isinstance(cur.get(p), dict):
                    cur[p] = {}
                cur = cur[p]
            cur[parts[-1]] = value
        else:
            nested = deep_merge(nested, {key: value})
    return nested


def _merge_text_provider(layered: dict[str, Any], value: ProviderArg) -> None:
    """Merge a provider/dict into models.extraction (extraction's LLM)."""
    if value is None:
        return
    if isinstance(value, (VisionProvider, AudioProvider, TextProvider)):
        payload = value.to_model_config()
    elif isinstance(value, dict):
        payload = value
    else:  # type: ignore[unreachable]
        raise TypeError(
            f"llm expects a Provider / dict / None, got {type(value).__name__}"
        )
    models = layered.setdefault("models", {})
    models["extraction"] = deep_merge(models.get("extraction", {}), payload)


def run(
    knowledge_dir: str | Path,
    *,
    processor: str = "extract",
    template: str | None = None,
    output: str | Path | None = None,
    llm: ProviderArg = None,
    config_overrides: dict[str, Any] | None = None,
    config_file: str | Path | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> ExtractResult:
    """
    Run a post-processor over a knowledge base's produced artefacts.

    Args:
        knowledge_dir: Knowledge-base root (the output_dir of `docingest run`).
            Reads sources/*.md (default) or chunks.jsonl per
            postprocess.input; never re-parses original documents.
        processor: Which post-processor to run. "extract" (only one today).
        template: For extract — a built-in template name ("doc_summary"),
            a project-local template name, or a path to a YAML file.
        output: Where to write results. Default:
            knowledge_dir/extracted/<template>.jsonl
        llm: LLM provider for the extraction calls (Provider / dict / None).
            None keeps YAML / env defaults (inherits models.defaults).
        config_overrides / config_file: same semantics as docingest.ingest.
        on_progress: callback(event) fired once per unit. Event keys:
            current / total / unit_id / status / pieces / failed_pieces.

    Returns:
        ExtractResult — counts, output path, and the records produced.
    """
    if processor != "extract":
        raise ValueError(
            f"Unknown processor {processor!r}. Available: 'extract'."
        )
    if template is None:
        raise ValueError("extract requires a template (e.g. template='doc_summary').")

    knowledge_path = Path(knowledge_dir)

    # Build config: knowledge_dir as output.dir so relative paths resolve.
    layered: dict[str, Any] = {"output": {"dir": str(knowledge_path)}, "postprocess": {}}
    if llm is not None:
        _merge_text_provider(layered, llm)
    if config_overrides:
        layered = deep_merge(layered, _normalize_overrides(config_overrides))
    config = load_config(
        project_config_path=Path(config_file) if config_file else None,
        cli_overrides=layered,
    )

    # Resolve template + build processor
    from .processors import ExtractProcessor, resolve_template_path
    from .schema_builder import load_template

    template_path = resolve_template_path(template, config)
    template_dict = load_template(template_path)
    proc = ExtractProcessor(template_dict)

    # Run
    runner = Runner(proc, config)
    run_result: RunResult = runner.run(knowledge_path, on_progress=on_progress)

    # Collect records (only successful units carry one)
    records = [
        {"unit_id": ur.unit_id, "source": ur.source, "record": ur.record}
        for ur in run_result.results
        if ur.record is not None
    ]

    # Resolve output path
    if output is not None:
        out_path = Path(output)
    else:
        subdir = get_nested(config, "postprocess.output_subdir", "extracted")
        tmpl_name = str(template_dict.get("name", "extract"))
        out_path = knowledge_path / subdir / f"{tmpl_name}.jsonl"

    # Write JSONL (atomic via .tmp + replace)
    written = _write_jsonl(out_path, records)

    return ExtractResult(
        processor=processor,
        template=str(template_dict.get("name", template)),
        units_total=run_result.units_total,
        units_ok=run_result.units_ok,
        units_failed=run_result.units_failed,
        pieces_total=run_result.pieces_total,
        pieces_failed=run_result.pieces_failed,
        elapsed_ms=run_result.elapsed_ms,
        output_path=str(written) if written else "",
        records=records,
        errors=run_result.errors,
    )


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path | None:
    """Write records as JSONL, atomically. Returns the path, or None on error."""
    import os
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        return path
    except OSError:
        return None


__all__ = ["run", "ExtractResult"]
