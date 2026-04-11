"""
CLI entry point — docingest command.

Usage:
  docingest ./docs/ -o ./knowledge/
  docingest ./docs/report.pdf ./docs/proposal.pptx
  docingest ./docs/ -c ./my-config.yaml
  docingest ./docs/ --no-chunks
  docingest ./docs/ --strategy heading
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# Load .env file if present (for API keys)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — user manages env vars manually

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .parsers import create_parser
from .chunkers import create_chunker
from .pipeline import run_pipeline

app = typer.Typer(
    name="docingest",
    help="Universal document preprocessing for RAG and Agentic Search.",
    add_completion=False,
    invoke_without_command=True,
)
console = Console()


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context):
    """Fallback: show help if no subcommand given."""
    if ctx.invoked_subcommand is None:
        # If arguments were passed without subcommand, treat as "run"
        pass


@app.command("run")
def main(
    inputs: list[Path] = typer.Argument(
        ...,
        help="Input files or directories to process.",
        exists=True,
    ),
    output: Path = typer.Option(
        "./knowledge",
        "-o", "--output",
        help="Output directory.",
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "-c", "--config",
        help="Path to project config YAML (overrides defaults).",
    ),
    no_chunks: bool = typer.Option(
        False,
        "--no-chunks",
        help="Disable chunking (only output Markdown files).",
    ),
    strategy: Optional[str] = typer.Option(
        None,
        "--strategy",
        help="Override chunking strategy: auto, heading, recursive, slide, sheet.",
    ),
    parallel: Optional[int] = typer.Option(
        None,
        "--parallel",
        help="Number of files to process in parallel.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Ignore incremental cache and re-process all files.",
    ),
) -> None:
    """Process documents for RAG and Agentic Search."""

    # Build CLI overrides
    cli_overrides: dict = {}

    cli_overrides["output"] = {"dir": str(output)}

    if no_chunks:
        cli_overrides.setdefault("chunking", {})["enabled"] = False

    if strategy:
        cli_overrides.setdefault("chunking", {})["strategy"] = strategy

    if parallel:
        cli_overrides.setdefault("performance", {})["parallel_files"] = parallel

    if force:
        cli_overrides.setdefault("incremental", {})["force"] = True

    # Load config
    try:
        config = load_config(
            project_config_path=config_file,
            cli_overrides=cli_overrides,
        )
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    # Create parser and chunker
    parser = create_parser(config)
    chunker = create_chunker(config) if config.get("chunking", {}).get("enabled", True) else None

    # Show start info
    console.print(f"\n[bold]DocIngest[/bold] v0.1.0")
    console.print(f"  Input:    {', '.join(str(p) for p in inputs)}")
    console.print(f"  Output:   {output}")
    console.print(f"  Chunking: {'disabled' if not chunker else config.get('chunking', {}).get('strategy', 'auto')}")
    console.print()

    # Run pipeline
    result = run_pipeline(
        input_paths=inputs,
        config=config,
        parser=parser,
        chunker=chunker,
    )

    # Show results
    _print_results(result)

    if result.failed > 0:
        raise typer.Exit(1)


def _print_results(result) -> None:
    """Print pipeline results as a rich table."""
    table = Table(title="Processing Results")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Total files", str(result.total_files))
    table.add_row("Successful", f"[green]{result.successful}[/green]")
    table.add_row("Failed", f"[red]{result.failed}[/red]" if result.failed else "0")
    table.add_row("Total chunks", str(result.total_chunks))
    table.add_row("Total tokens (est.)", f"{result.total_tokens:,}")
    table.add_row("Elapsed", f"{result.elapsed_ms}ms")

    console.print(table)

    # Quality summary (if quality_report was generated)
    quality = getattr(result, "quality", None) or {}
    if quality.get("total_files", 0) > 0:
        q = quality.get("total_questions", 0)
        u = quality.get("total_unreadable", 0)
        issues = quality.get("files_with_issues", 0)
        total = quality.get("total_files", 0)
        score = quality.get("quality_score", 1.0)

        if q == 0 and u == 0:
            console.print(
                f"\n[green]✓ Vision quality:[/green] clean ({total} files, "
                f"zero uncertainty markers)"
            )
        else:
            color = "green" if score >= 0.9 else ("yellow" if score >= 0.7 else "red")
            console.print(
                f"\n[{color}]Vision quality:[/{color}] "
                f"{issues}/{total} files have markers "
                f"— [yellow]{q}[/yellow] partial reads [?], "
                f"[red]{u}[/red] unreadable "
                f"(score: {score:.2f})"
            )
            # Show top 3 files with most issues for quick triage
            files_with_issues = quality.get("files", [])
            if files_with_issues:
                top = sorted(
                    files_with_issues,
                    key=lambda f: f["question_count"] + f["unreadable_count"] * 2,
                    reverse=True,
                )[:3]
                console.print("  [dim]Top files to review:[/dim]")
                for f in top:
                    name = Path(f["file"]).name
                    console.print(
                        f"    [dim]•[/dim] {name}: "
                        f"{f['question_count']} [?], {f['unreadable_count']} [unreadable]"
                    )

    if result.errors:
        console.print(f"\n[yellow]Errors ({len(result.errors)}):[/yellow]")
        for err in result.errors:
            console.print(f"  [red]✗[/red] {err['file']}: {err['error']}")

    console.print()


# ---------------------------------------------------------------------------
# Refine subcommand
# ---------------------------------------------------------------------------

@app.command("refine")
def refine_cmd(
    files: list[Path] = typer.Argument(
        ...,
        help="Markdown files to refine (e.g. knowledge/sources/spec.md)",
        exists=True,
    ),
    output_dir: Optional[Path] = typer.Option(
        None,
        "-o", "--output",
        help="Base output directory (default: parent of sources/).",
    ),
    skill: Optional[str] = typer.Option(
        None,
        "--skill",
        help="SKILL name to use (default: refine_default).",
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "-c", "--config",
        help="Path to project config YAML.",
    ),
) -> None:
    """Refine Markdown files for human readability (AI-powered)."""
    from .refine import refine_files

    config = load_config(project_config_path=config_file)

    # Infer output_dir from first file if not specified
    # e.g. knowledge/sources/xxx.md → knowledge/
    if output_dir is None:
        first = files[0].resolve()
        if first.parent.name == "sources":
            output_dir = first.parent.parent
        else:
            output_dir = first.parent

    console.print(f"\n[bold]DocIngest Refine[/bold]")
    console.print(f"  Files:  {len(files)}")
    console.print(f"  Skill:  {skill or config.get('refine', {}).get('default_skill', 'refine_default')}")
    console.print(f"  Output: {output_dir / config.get('refine', {}).get('output_dir', 'readable')}")
    console.print()

    results = refine_files(files, config, output_dir, skill)

    # Print results
    refined_count = sum(1 for r in results if not r["skipped"])
    skipped_count = sum(1 for r in results if r["skipped"])

    table = Table(title="Refine Results")
    table.add_column("File", style="bold")
    table.add_column("Status")
    table.add_column("Tokens", justify="right")

    for r in results:
        name = Path(r["source"]).name
        if r["skipped"]:
            table.add_row(name, f"[yellow]skipped[/yellow]", r.get("warning", ""))
        else:
            table.add_row(
                name,
                f"[green]refined[/green]",
                f"{r['tokens_in']:,} → {r['tokens_out']:,}",
            )

    console.print(table)

    if skipped_count:
        console.print(f"\n[yellow]Skipped {skipped_count} file(s)[/yellow]")
        for r in results:
            if r["skipped"]:
                console.print(f"  {Path(r['source']).name}: {r['warning']}")

    console.print(f"\n[green]Refined: {refined_count}[/green] / {len(results)} files\n")


if __name__ == "__main__":
    app()
