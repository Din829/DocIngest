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
)
console = Console()


@app.command()
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

    if result.errors:
        console.print(f"\n[yellow]Errors ({len(result.errors)}):[/yellow]")
        for err in result.errors:
            console.print(f"  [red]✗[/red] {err['file']}: {err['error']}")

    console.print()


if __name__ == "__main__":
    app()
