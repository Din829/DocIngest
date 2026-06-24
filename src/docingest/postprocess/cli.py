"""
``docingest extract`` command — typer entrypoint for template-driven
structured extraction (the postprocess layer's first processor).

Wired into the main app conditionally in cli.py:
    try: from .postprocess.cli import extract_cmd; app.command("extract")(extract_cmd)
    except ImportError: pass
so it only appears when the [postprocess] extra is installed. Style mirrors
the rest of cli.py: banner / progress / errors → stderr, JSON → stdout.
"""

from __future__ import annotations

import json as json_mod
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def extract_cmd(
    knowledge_dir: Path = typer.Argument(
        ...,
        help="Knowledge base root (the output_dir of an earlier `docingest run`).",
        exists=True, file_okay=False, dir_okay=True,
    ),
    template: str = typer.Option(
        "doc_summary",
        "-t", "--template",
        help="Template: a built-in name (doc_summary), a project-local "
             "template name, or a path to a YAML file.",
    ),
    output: Optional[Path] = typer.Option(
        None, "-o", "--output",
        help="Output JSONL path. Default: <kb>/extracted/<template>.jsonl",
    ),
    input_mode: Optional[str] = typer.Option(
        None, "--input",
        help="What to extract from: 'sources' (whole md files) or 'chunks' "
             "(chunks.jsonl lines). Omit to use postprocess.input from config "
             "(defaults to 'sources').",
    ),
    parallel: Optional[int] = typer.Option(
        None, "--parallel",
        help="Parallel LLM calls for oversized documents (default from config).",
    ),
    config_file: Optional[Path] = typer.Option(
        None, "-c", "--config", help="Path to project docingest.yaml.",
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit the run summary as JSON to stdout (for agent consumption).",
    ),
) -> None:
    """Extract a structured record per document using a YAML template."""
    from . import api as pp_api

    err_console.print("\n[bold]DocIngest Extract[/bold]")
    err_console.print(f"  Knowledge dir: {knowledge_dir}")
    err_console.print(f"  Template:      {template}")
    err_console.print()

    overrides: dict = {}
    if input_mode:
        overrides["postprocess.input"] = input_mode
    if parallel is not None:
        overrides["postprocess.parallel"] = parallel

    state = {"current": 0, "total": 0}

    def _on_progress(event: dict) -> None:
        state["current"] = int(event.get("current", state["current"]))
        state["total"] = int(event.get("total", state["total"]))
        err_console.print(
            f"  [{state['current']:>4}/{state['total']:>4}] "
            f"{str(event.get('unit_id', ''))[:48]:<48} "
            f"[dim]{event.get('status', '')}"
            f"{'' if event.get('failed_pieces', 0) == 0 else ' (' + str(event['failed_pieces']) + ' piece-fail)'}"
            f"[/dim]",
            end="\r",
        )

    try:
        result = pp_api.run(
            knowledge_dir,
            processor="extract",
            template=template,
            output=output,
            config_overrides=overrides or None,
            config_file=config_file,
            on_progress=_on_progress,
        )
    except Exception as e:
        err_console.print(f"\n[red]Extract failed:[/red] {type(e).__name__}: {e}")
        raise typer.Exit(code=1)

    err_console.print()  # finish the \r progress line

    if json_output:
        print(json_mod.dumps({
            "processor": result.processor,
            "template": result.template,
            "units_total": result.units_total,
            "units_ok": result.units_ok,
            "units_failed": result.units_failed,
            "pieces_total": result.pieces_total,
            "pieces_failed": result.pieces_failed,
            "elapsed_ms": result.elapsed_ms,
            "output_path": result.output_path,
        }, ensure_ascii=False))
    else:
        err_console.print(
            f"[green]✓[/green] {result.units_ok}/{result.units_total} documents "
            f"extracted ({result.pieces_failed} piece-failures) in "
            f"{result.elapsed_ms / 1000:.1f}s"
        )
        err_console.print(f"  → {result.output_path}")
