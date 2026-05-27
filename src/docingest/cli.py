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

from .config import load_config, get_nested, ConfigError
from .parsers import create_parser
from .chunkers import create_chunker
from .pipeline import run_pipeline

app = typer.Typer(
    name="docingest",
    help="Universal document preprocessing for RAG and Agentic Search.",
    add_completion=False,
    invoke_without_command=True,
)

# Optional GraphRAG subcommand — registered only when the docingest.graph
# subpackage imports cleanly (i.e. the [graph] extras are installed). The
# graph subpackage's own __init__ defers heavy imports, so this try/except
# is cheap when extras ARE installed and entirely silent when they aren't.
# Failure here must never block the rest of the CLI from working.
try:
    from .graph.cli import graph_app
    app.add_typer(graph_app, name="graph")
except ImportError:
    pass

console = Console()
# Dedicated stderr console for banners, progress, and errors. Keeps stdout
# clean for `--json` consumers (agents / subprocess callers) without
# changing behaviour for interactive users — Rich still renders to a
# terminal, just the other stream.
err_console = Console(stderr=True)


def _load_config_or_exit(
    project_config_path: Path | None = None,
    cli_overrides: dict | None = None,
) -> dict:
    """
    Wrap ``load_config`` so config problems surface as a friendly stderr
    message + exit code 1, instead of either an opaque traceback (yaml
    syntax error) or a silent fall-through (mistyped -c path).

    Library callers (docingest.api / direct imports) keep the raw
    ConfigError contract — only the CLI converts to exit-code semantics.
    """
    try:
        return load_config(
            project_config_path=project_config_path,
            cli_overrides=cli_overrides,
        )
    except ConfigError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except FileNotFoundError as e:
        # Reachable only when the bundled default.yaml is missing (corrupt
        # install). User-facing project-config "not found" is now a
        # ConfigError, handled above.
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


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
    output: Optional[Path] = typer.Option(
        None,
        "-o", "--output",
        help=(
            "Output directory. Optional for SINGLE-input runs "
            "(auto-derives to ./knowledge/<input_name>/). REQUIRED for "
            "multi-input runs — each knowledge base gets its own root "
            "so runs do not silently pollute ./knowledge/."
        ),
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
    yes: bool = typer.Option(
        False,
        "-y", "--yes", "--acknowledge-large",
        help=(
            "Proceed even when safety (strict mode) would abort the run due "
            "to oversized files or over-budget per-run totals. Use after "
            "reviewing the estimate; in warn mode (default) this flag is "
            "a no-op."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit the run summary as JSON to stdout (for agent / subprocess "
            "consumption). Banner, errors and progress info still go to "
            "stderr. Exit code is unchanged: 0 success, 1 failures, 2 "
            "safety abort."
        ),
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help=(
            "Stream INFO-level docingest pipeline progress to stderr (Vision "
            "batched calls, per-batch describe, page-image generation, etc.). "
            "litellm / httpx / urllib3 stay at WARNING regardless — those "
            "tools' INFO logs are pure noise (one 'Wrapper: Completed Call' "
            "per LLM call). Independent of --json: stdout JSON output stays "
            "clean. A full INFO-level run.log file is written to <output>/ "
            "every run regardless of -v, so post-mortem analysis works "
            "without re-running with -v."
        ),
    ),
) -> None:
    """Process documents for RAG and Agentic Search."""
    # Logging policy:
    #   - docingest.* loggers always at INFO (so the run.log file captures
    #     full progress regardless of -v).
    #   - Third-party loggers (LiteLLM / httpx / urllib3 / openpyxl / PIL)
    #     forced to WARNING — their INFO is 4-6 noise lines per LLM call
    #     and drowns out docingest's own progress.
    #   - StreamHandler attached only when -v is set, so console stays quiet
    #     unless the user opts in. FileHandler is wired AFTER we resolve
    #     output_dir below (file path needs to exist first).
    import logging
    import sys
    logging.getLogger("docingest").setLevel(logging.INFO)
    for noisy in ("LiteLLM", "litellm", "httpx", "httpcore",
                  "openpyxl", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if verbose:
        _stream_handler = logging.StreamHandler(sys.stderr)
        _stream_handler.setLevel(logging.INFO)
        _stream_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        logging.getLogger("docingest").addHandler(_stream_handler)

    # Resolve output directory.
    # Policy:
    #   - explicit -o  → honour verbatim (user owns the decision).
    #   - single input → auto-derive ./knowledge/<stem>/ so repeated runs
    #                    hit the same folder and the incremental cache
    #                    keeps working.
    #   - multi input  → REFUSE to auto-derive. Writing to ./knowledge/
    #                    root would silently merge unrelated runs into
    #                    one knowledge base, and auto-naming from N
    #                    filenames is ambiguous (which stem wins? how is
    #                    order surfaced? what about url/mixed inputs?).
    #                    Requiring -o forces the user to name the
    #                    knowledge base themselves.
    # typer's exists=True on Argument already guarantees every input is
    # an existing local path when we reach this point, so no extra
    # existence check is needed.
    if output is None:
        if len(inputs) == 1:
            output = Path("./knowledge") / inputs[0].stem
        else:
            err_console.print(
                "[red]Error:[/red] multiple inputs require an explicit "
                "[bold]-o / --output[/bold] flag so each knowledge base "
                "gets its own root.\n"
                "  Example:\n"
                "    [bold]docingest run a.pdf b.pdf -o "
                "./knowledge/my-project/[/bold]"
            )
            raise typer.Exit(1)

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

    # Load config — friendly errors on bad path / yaml syntax / non-mapping
    # top-level. See _load_config_or_exit for the exit-code semantics.
    config = _load_config_or_exit(
        project_config_path=config_file,
        cli_overrides=cli_overrides,
    )

    # Create parser and chunker
    parser = create_parser(config)
    chunker = create_chunker(config) if config.get("chunking", {}).get("enabled", True) else None

    # Attach FileHandler for run.log AFTER output_dir is finalized. Always on
    # (independent of -v) so post-mortem analysis works without re-running with
    # -v. Appends across runs so multiple `docingest run` invocations against
    # the same output dir leave a chronological trace — the file boundary is
    # the per-run header banner printed below + the "Processing Results"
    # table footer, both of which the FileHandler captures.
    output.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.FileHandler(
        output / "run.log",
        mode="a",  # append — multi-run history is more useful than overwrite
        encoding="utf-8",
    )
    _file_handler.setLevel(logging.INFO)
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    ))
    logging.getLogger("docingest").addHandler(_file_handler)

    # Show start info — always to stderr so `--json` consumers get a clean
    # stdout. Interactive users see banner + table on the terminal as before.
    err_console.print(f"\n[bold]DocIngest[/bold] v0.1.0")
    err_console.print(f"  Input:    {', '.join(str(p) for p in inputs)}")
    err_console.print(f"  Output:   {output}")
    err_console.print(f"  Chunking: {'disabled' if not chunker else config.get('chunking', {}).get('strategy', 'auto')}")
    err_console.print()
    # Mirror the banner to run.log so file readers see the same context as
    # interactive users (which run, which inputs, which output dir).
    logging.getLogger("docingest").info(
        f"=== run start: inputs={[str(p) for p in inputs]} output={output} "
        f"chunking={'disabled' if not chunker else config.get('chunking', {}).get('strategy', 'auto')} ==="
    )

    # Run pipeline. CLI opts in to the SIGINT handler so Ctrl+C stops
    # gracefully (finish current file, write aggregates, exit 130).
    # Library callers default to install_signal_handler=False so they
    # keep their own signal handling.
    result = run_pipeline(
        input_paths=inputs,
        config=config,
        parser=parser,
        chunker=chunker,
        acknowledge_large=yes,
        install_signal_handler=True,
    )

    # Show results — JSON to stdout for agents, Rich table to stderr-attached
    # console for humans. Exit codes are unchanged regardless of mode.
    if json_output:
        _print_results_json(result)
    else:
        _print_results(result)

    # Safety strict-mode abort → exit 2, distinct from generic failure (1).
    # Using a separate code lets scripts/CI distinguish "budget exceeded,
    # re-run with --yes" from "something actually broke".
    if result.safety.get("aborted"):
        raise typer.Exit(2)

    # Graceful interrupt (Ctrl+C between files) → exit 130 (128 + SIGINT).
    # Aggregates were already written for completed files; rerun resumes
    # via incremental cache.
    if getattr(result, "interrupted", False):
        if not json_output:
            console.print(
                "\n[yellow]Run interrupted — partial results saved at "
                f"{output}. Rerun to resume from cache.[/yellow]"
            )
        raise typer.Exit(130)

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

    # Non-fatal warnings — page-cap hits, OCR fallbacks, etc. Files all
    # processed successfully, but with a quality compromise that's invisible
    # unless we surface it here. Printed BEFORE safety so the user sees
    # quality signals before reading the rest of the table.
    warnings = getattr(result, "warnings", None) or []
    if warnings:
        # Group by file so a 50-file batch with the same warning shows once,
        # not 50 times. Keep at most 5 distinct warning messages per file in
        # the rendered output — full list stays on result.warnings for
        # programmatic consumers (Mplat / agents).
        by_file: dict[str, list[str]] = {}
        for w in warnings:
            by_file.setdefault(w["file"], []).append(w["message"])
        console.print(
            f"\n[yellow]Warnings:[/yellow] {len(warnings)} non-fatal "
            f"issue(s) across {len(by_file)} file(s) — content may be incomplete:"
        )
        for fname, msgs in list(by_file.items())[:10]:  # cap at 10 files for tidy output
            console.print(f"  [yellow]•[/yellow] {fname}")
            for m in msgs[:5]:
                console.print(f"      {m}")
        if len(by_file) > 10:
            console.print(f"  [dim]... and {len(by_file) - 10} more file(s) "
                          f"(see result.stats['warnings'])[/dim]")

    # Safety summary (if Phase 0 ran and found violations). Printed early
    # so users see the abort reason before the rest of the diagnostics.
    safety = getattr(result, "safety", None) or {}
    if safety.get("violations"):
        n = len(safety["violations"])
        mode = safety.get("mode", "warn")
        aborted = safety.get("aborted", False)
        color = "red" if aborted else "yellow"
        status = "ABORTED — nothing was processed" if aborted else "proceeding"
        console.print(
            f"\n[{color}]Safety:[/{color}] {n} violation(s), mode={mode} — {status}"
        )
        try:
            from docingest.safety import format_violations
            console.print(format_violations(safety["violations"]))
        except Exception:
            # Rendering failure must not hide the rest of the output.
            pass
        if aborted:
            console.print(
                "[yellow]→ Re-run with [bold]--yes[/bold] to proceed, "
                "or raise a threshold in [bold]safety.per_file[/bold] / "
                "[bold]safety.per_run[/bold] config.[/yellow]"
            )

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

    # LLM API token usage summary
    usage = getattr(result, "token_usage", None) or {}
    if usage.get("total_tokens", 0) > 0 or usage.get("total_cache_hits", 0) > 0:
        total = usage.get("total_tokens", 0)
        prompt = usage.get("total_prompt_tokens", 0)
        completion = usage.get("total_completion_tokens", 0)
        calls = usage.get("total_calls", 0)
        hits = usage.get("total_cache_hits", 0)
        console.print(
            f"\n[bold]LLM usage:[/bold] "
            f"{total:,} tokens "
            f"([dim]{prompt:,} in + {completion:,} out[/dim])"
            f" — {calls} API call{'s' if calls != 1 else ''}"
            + (f", {hits} cache hit{'s' if hits != 1 else ''}" if hits else "")
        )

    console.print()


def _print_results_json(result) -> None:
    """Print pipeline results as a single JSON object to stdout.

    Field shape mirrors `IngestResult.stats` (docingest.api) and the MCP
    `run` tool's return value, so CLI subprocess callers, library callers,
    and MCP callers all see the same keys.

    Banner / errors / quality summary still go to stderr via the human
    path; this function only writes the machine-consumable summary.
    """
    payload: dict = {
        "total_files": result.total_files,
        "successful": result.successful,
        "failed": result.failed,
        "total_chunks": result.total_chunks,
        "total_tokens": result.total_tokens,
        "elapsed_ms": result.elapsed_ms,
        "errors": list(result.errors),
        "quality": dict(result.quality),
        "token_usage": dict(result.token_usage),
        "safety": dict(result.safety),
    }
    if payload["safety"].get("aborted"):
        payload["status"] = "aborted_by_safety"
    # Plain stdout write — no Rich, no color, no markup. Agents parse this.
    import json as _json
    import sys as _sys
    _sys.stdout.write(_json.dumps(payload, ensure_ascii=False, indent=2))
    _sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# Inspect subcommand
# ---------------------------------------------------------------------------

@app.command("inspect")
def inspect_cmd(
    inputs: list[Path] = typer.Argument(
        ...,
        help="Files or directories to inspect.",
        exists=True,
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "-c", "--config",
        help="Path to project config YAML.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output as JSON (for Agent/MCP consumption).",
    ),
) -> None:
    """Inspect documents before processing — reports size, pages, and recommendations."""
    import json as json_mod
    from .inspect import inspect_files

    config = _load_config_or_exit(project_config_path=config_file)
    results = inspect_files(inputs, config)

    if json_output:
        # Plain stdout write — bypass Rich so subprocess callers parsing
        # this JSON don't trip on Rich's terminal-width wrapping or markup
        # interpretation of multi-byte filenames. Same pattern as
        # `run --json` via `_print_results_json`.
        import sys as _sys
        _sys.stdout.write(json_mod.dumps(results, indent=2, ensure_ascii=False))
        _sys.stdout.write("\n")
        return

    # Rich table output
    table = Table(title="Document Inspection")
    table.add_column("File", style="bold", max_width=40)
    table.add_column("Format")
    table.add_column("Size", justify="right")
    table.add_column("Pages", justify="right")
    table.add_column("Recommendation")

    total_pages = 0
    for r in results:
        pages = r.get("pages")
        pages_str = str(pages) if pages is not None else "?"
        if r.get("pages_estimated"):
            pages_str += " (est)"
        if pages:
            total_pages += pages

        size_str = f"{r['size_mb']:.1f}MB" if r['size_mb'] >= 1 else f"{r['size_mb']*1024:.0f}KB"

        rec = r.get("recommendation", "")
        rec_style = "[green]" if rec == "Ready" else "[yellow]"
        rec_display = f"{rec_style}{rec}[/{rec_style[1:]}"

        table.add_row(r["name"], r["format"], size_str, pages_str, rec_display)

    console.print(table)

    max_vision = get_nested(config, "parsing.vision.max_pages", 50)
    vision_est = min(total_pages, int(max_vision)) if max_vision else total_pages
    console.print(
        f"\n  Total: {len(results)} file(s), ~{total_pages} pages, "
        f"~{vision_est} Vision API calls"
        f"{f' (capped at {max_vision})' if max_vision and total_pages > int(max_vision) else ''}"
    )
    console.print()


# ---------------------------------------------------------------------------
# Doctor subcommand
# ---------------------------------------------------------------------------

@app.command("doctor")
def doctor_cmd() -> None:
    """Check environment: packages, external tools, API keys."""
    from .doctor import run_doctor, print_doctor

    config = _load_config_or_exit()
    results = run_doctor(config)
    print_doctor(results)


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
        help="SKILL name (default: refine_default). Built-in: refine_default, "
             "refine_faithful, refine_html. Skills containing 'html' emit .html.",
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "-c", "--config",
        help="Path to project config YAML.",
    ),
) -> None:
    """Refine Markdown files for human readability (AI-powered)."""
    from .refine import refine_files

    config = _load_config_or_exit(project_config_path=config_file)

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
