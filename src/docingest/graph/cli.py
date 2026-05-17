"""
``docingest graph`` subcommand group — typer entrypoint for the GraphRAG layer.

Wired into the main ``docingest`` typer app conditionally: ``cli.py`` does
``try: from .graph.cli import graph_app; app.add_typer(graph_app, name="graph")
except ImportError: pass`` so the subcommand only appears when the
``[graph]`` extras are installed (or at least lightrag-hku itself is).

Three commands matching the Python facade:
    docingest graph build  ./kb/  [--mode full|vector_only] [--force] ...
    docingest graph query  "..."  --kb ./kb/ [--mode hybrid|local|...] ...
    docingest graph status ./kb/  [--json]

Style mirrors the rest of cli.py:
    - banner / progress / errors → stderr (err_console)
    - JSON → stdout (for agent / subprocess consumers)
    - friendly ConfigError handling via _load_config_or_exit pattern
"""

from __future__ import annotations

import json as json_mod
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import api as graph_api


graph_app = typer.Typer(
    name="graph",
    help=(
        "GraphRAG layer (optional). Build a knowledge graph + community "
        "summaries on top of an existing knowledge base, then run "
        "global / local / hybrid queries. Requires `pip install -e \".[graph]\"`."
    ),
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

@graph_app.command("build")
def build_cmd(
    knowledge_dir: Path = typer.Argument(
        ...,
        help="Knowledge base root (the output_dir of an earlier `docingest run`).",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    mode: Optional[str] = typer.Option(
        None,
        "--mode",
        help="Override graph.mode: 'vector_only' (cheap, single/two-hop) or "
             "'full' (adds community detection + global queries).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Ignore the graph extraction cache and rebuild from scratch.",
    ),
    enrich_chunks: bool = typer.Option(
        False,
        "--enrich-chunks",
        help="After build completes, also write chunks_enriched.jsonl "
             "with per-chunk entity descriptions injected into both text "
             "(for vector RAG) and metadata (for filter / hybrid). "
             "Original chunks.jsonl is never modified.",
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "-c", "--config",
        help="Path to project docingest.yaml.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the build summary as JSON to stdout (for agent consumption).",
    ),
) -> None:
    """Build a knowledge graph on top of an existing knowledge base."""

    err_console.print(f"\n[bold]DocIngest Graph Build[/bold]")
    err_console.print(f"  Knowledge dir: {knowledge_dir}")
    err_console.print(f"  Mode:          {mode or '(from config)'}")
    err_console.print(f"  Force:         {force}")
    err_console.print()

    # Per-chunk progress events on stderr — same convention as run_pipeline's
    # on_progress in the main CLI. We update a single status line so a 10K-
    # chunk build doesn't fill the terminal.
    state = {"current": 0, "total": 0}

    def _on_progress(event: dict) -> None:
        state["current"] = int(event.get("current", state["current"]))
        state["total"] = int(event.get("total", state["total"]))
        # \r overwrite — Rich handles ANSI cursor movement portably.
        err_console.print(
            f"  [{state['current']:>5}/{state['total']:>5}] "
            f"{event.get('chunk_id', '')[:50]:<50} "
            f"[dim]{event.get('status', '')}[/dim]",
            end="\r",
        )

    # CLI --enrich-chunks forces graph.enrich_chunks.enabled=true for this
    # call only. Persists nothing — to make it the default, set the same
    # key in docingest.yaml.
    cli_overrides: dict | None = None
    if enrich_chunks:
        cli_overrides = {"graph.enrich_chunks.enabled": True}

    try:
        result = graph_api.build(
            knowledge_dir,
            mode=mode,
            force=force,
            config_file=config_file,
            config_overrides=cli_overrides,
            on_progress=_on_progress,
        )
    except ImportError as e:
        # Most common failure: lightrag-hku not installed.
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except (ValueError, FileNotFoundError) as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    err_console.print()  # newline after progress line

    if json_output:
        payload = {
            "backend": result.backend,
            "mode": result.mode,
            "entities_count": result.entities_count,
            "relations_count": result.relations_count,
            "communities_count": result.communities_count,
            "chunks_processed": result.chunks_processed,
            "chunks_skipped_cached": result.chunks_skipped_cached,
            "elapsed_ms": result.elapsed_ms,
            "output_dir": result.output_dir,
            "errors": result.errors,
        }
        sys.stdout.write(json_mod.dumps(payload, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
    else:
        table = Table(title="Graph Build Result")
        table.add_column("Field", style="bold")
        table.add_column("Value", justify="right")
        table.add_row("Backend", result.backend)
        table.add_row("Mode", result.mode)
        table.add_row("Entities", f"{result.entities_count:,}")
        table.add_row("Relations", f"{result.relations_count:,}")
        table.add_row("Communities", f"{result.communities_count:,}")
        table.add_row("Chunks extracted", str(result.chunks_processed))
        table.add_row("Chunks cached", str(result.chunks_skipped_cached))
        # Enrichment stats only show when the toggle was on for this run.
        if "enriched_output_path" in result.stats:
            written = result.stats.get("enriched_chunks_written", 0)
            avg = result.stats.get("enriched_avg_entities_per_chunk", 0.0)
            total = result.stats.get("enriched_total_entities_injected", 0)
            table.add_row(
                "Enriched chunks",
                f"{written:,} (avg {avg} ent/chunk, {total:,} total)",
            )
        table.add_row("Elapsed", f"{result.elapsed_ms} ms")
        console.print(table)
        if "enriched_output_path" in result.stats:
            console.print(
                f"  [dim]→ enriched output:[/dim] {result.stats['enriched_output_path']}"
            )

        if result.errors:
            err_console.print(f"\n[yellow]Warnings ({len(result.errors)}):[/yellow]")
            for msg in result.errors:
                err_console.print(f"  [yellow]·[/yellow] {msg}")
            # Hard-failure cases (chunks_path missing, ainsert blew up) are
            # surfaced as errors but build() returned — propagate via exit
            # code so scripts notice.
            raise typer.Exit(1)


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

@graph_app.command("query")
def query_cmd(
    question: str = typer.Argument(
        ...,
        help="Natural-language question to ask the graph.",
    ),
    knowledge_dir: Path = typer.Option(
        ...,
        "--kb", "--knowledge-dir",
        help="Knowledge base root containing the previously-built graph.",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    mode: str = typer.Option(
        "hybrid",
        "--mode",
        help="Query mode: naive | local | global | hybrid | mix. "
             "vector_only graphs accept only naive / local.",
    ),
    top_k: Optional[int] = typer.Option(
        None,
        "--top-k",
        help="Backend-specific retrieval cutoff. None = backend default.",
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "-c", "--config",
        help="Path to project docingest.yaml.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the query result as JSON (for agent consumption).",
    ),
) -> None:
    """Query a previously-built graph."""

    try:
        result = graph_api.query(
            question,
            knowledge_dir=knowledge_dir,
            mode=mode,
            top_k=top_k,
            config_file=config_file,
        )
    except ImportError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except (ValueError, FileNotFoundError) as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    # Backend signals soft failure (incl. LightRAG-internal silent
    # failures) via result.stats["error"]. Surface it visibly so users
    # don't mistake an empty answer for "no relevant info found".
    backend_error = result.stats.get("error") if result.stats else None

    if json_output:
        payload = {
            "answer": result.answer,
            "mode_used": result.mode_used,
            "elapsed_ms": result.elapsed_ms,
            "output_dir": result.output_dir,
        }
        if backend_error:
            payload["error"] = backend_error
        sys.stdout.write(json_mod.dumps(payload, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        # Non-zero exit code makes shell pipelines / agents notice.
        if backend_error:
            raise typer.Exit(1)
    else:
        err_console.print(
            f"\n[bold]Mode:[/bold] {result.mode_used}  "
            f"[dim]({result.elapsed_ms} ms)[/dim]\n"
        )
        if backend_error:
            err_console.print(f"[red]Backend error:[/red] {backend_error}\n")
            raise typer.Exit(1)
        # Answer goes to stdout so users can pipe `docingest graph query ... > out.md`
        sys.stdout.write(result.answer + "\n")


# ---------------------------------------------------------------------------
# enrich (standalone)
# ---------------------------------------------------------------------------

@graph_app.command("enrich")
def enrich_cmd(
    knowledge_dir: Path = typer.Argument(
        ...,
        help="Knowledge base root with an existing graph (built by `graph build`).",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "-c", "--config",
        help="Path to project docingest.yaml.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the enrichment summary as JSON (for agent consumption).",
    ),
) -> None:
    """
    Generate chunks_enriched.jsonl from an already-built graph.

    Reads graph/ artefacts (entities, descriptions, source-id map) and
    rewrites each chunk in chunks.jsonl into a sibling enriched file
    with per-chunk entity descriptions injected. The original
    chunks.jsonl is NEVER modified — delete the enriched file at any
    time to drop back to the original behaviour.

    Use this when the graph was built earlier without --enrich-chunks
    and you've decided you want enrichment now. No LLM / embedding
    calls are made — it's a pure replay over on-disk graph products.
    """

    try:
        result = graph_api.enrich_chunks(
            knowledge_dir,
            config_file=config_file,
        )
    except (ValueError, FileNotFoundError) as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if json_output:
        payload = {
            "written_path": result.written_path,
            "chunks_total": result.chunks_total,
            "chunks_enriched": result.chunks_enriched,
            "chunks_unchanged": result.chunks_unchanged,
            "total_entities_injected": result.total_entities_injected,
            "avg_entities_per_chunk": result.avg_entities_per_chunk,
            "elapsed_ms": result.elapsed_ms,
            "errors": result.errors,
        }
        sys.stdout.write(json_mod.dumps(payload, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return

    table = Table(title="Chunk Enrichment Result")
    table.add_column("Field", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Output", result.written_path or "(not written)")
    table.add_row("Chunks total", str(result.chunks_total))
    table.add_row("Chunks enriched", f"[green]{result.chunks_enriched}[/green]")
    table.add_row("Chunks unchanged", str(result.chunks_unchanged))
    table.add_row("Total entities", f"{result.total_entities_injected:,}")
    table.add_row("Avg entities / chunk", str(result.avg_entities_per_chunk))
    table.add_row("Elapsed", f"{result.elapsed_ms} ms")
    console.print(table)

    if result.errors:
        err_console.print(f"\n[yellow]Warnings ({len(result.errors)}):[/yellow]")
        for msg in result.errors:
            err_console.print(f"  [yellow]·[/yellow] {msg}")
        # Soft errors only — don't exit non-zero. Hard input errors above
        # already exited 1; if we got here the file was written.


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@graph_app.command("status")
def status_cmd(
    knowledge_dir: Path = typer.Argument(
        ...,
        help="Knowledge base root.",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "-c", "--config",
        help="Path to project docingest.yaml.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit status as JSON.",
    ),
) -> None:
    """Show whether a graph has been built and basic statistics."""

    info = graph_api.status(knowledge_dir, config_file=config_file)

    if json_output:
        payload = {
            "built": info.built,
            "backend": info.backend,
            "mode": info.mode,
            "entities_count": info.entities_count,
            "relations_count": info.relations_count,
            "communities_count": info.communities_count,
            "last_built_at": info.last_built_at,
            "embedding_model": info.embedding_model,
            "embedding_dimension": info.embedding_dimension,
            "output_dir": info.output_dir,
        }
        sys.stdout.write(json_mod.dumps(payload, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return

    if not info.built:
        console.print(
            f"\n[yellow]No graph built[/yellow] at {info.output_dir}.\n"
            f"  Run [bold]docingest graph build {knowledge_dir}[/bold] to build one.\n"
        )
        return

    table = Table(title="Graph Status")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Backend", info.backend)
    table.add_row("Mode", info.mode or "(unknown)")
    table.add_row("Entities", f"{info.entities_count:,}")
    table.add_row("Relations", f"{info.relations_count:,}")
    table.add_row("Communities", f"{info.communities_count:,}")
    table.add_row("Last built", info.last_built_at or "(no manifest)")
    table.add_row(
        "Embedding",
        f"{info.embedding_model or '?'} (dim={info.embedding_dimension or '?'})",
    )
    console.print(table)


__all__ = ["graph_app"]
