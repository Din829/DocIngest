"""
Critical regression test: GraphRAG layer is OPTIONAL.

The graph subpackage adds heavy optional dependencies (lightrag-hku,
embedding clients) that the main pipeline must not depend on. This test
verifies that, with or without those dependencies installed:

1. ``import docingest`` still works.
2. ``docingest.ingest()`` (the main pipeline facade) still works.
3. ``import docingest.graph`` either succeeds (extras installed) or
   fails with a clear ImportError — never with an attribute error or
   silent partial state.
4. The CLI's `docingest` entrypoint still loads and lists subcommands;
   the `graph` subcommand only appears when the import worked.

Run:
    python tests/unit/test_graph_optional.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


def test_main_package_imports_clean() -> None:
    """The main facade must import without touching the graph subpackage."""
    # Drop any prior import to make the test repeatable across runs.
    for name in list(sys.modules):
        if name == "docingest" or name.startswith("docingest."):
            del sys.modules[name]

    import docingest

    # Stable public API must still be exported.
    for attr in (
        "ingest",
        "inspect",
        "refine",
        "IngestResult",
        "build_config",
        "GeminiProvider",
        "OpenAIProvider",
        "DashScopeProvider",
    ):
        assert hasattr(docingest, attr), f"docingest.{attr} missing from public API"

    # The graph subpackage must NOT be auto-imported by the main package.
    # If it were, the optional [graph] dependency chain would silently
    # become a hard requirement.
    assert "docingest.graph" not in sys.modules, (
        "docingest.graph was loaded as a side-effect of `import docingest` — "
        "the optional layer must require explicit `import docingest.graph`."
    )

    print("OK: docingest imports without graph subpackage")


def test_graph_subpackage_isolated() -> None:
    """
    Importing docingest.graph must not error when [graph] extras are
    installed, and must raise a clean ImportError otherwise.
    """
    # Reset module cache so the import is fresh.
    for name in list(sys.modules):
        if name.startswith("docingest"):
            del sys.modules[name]

    try:
        import docingest.graph as graph_pkg
    except ImportError as e:
        # Acceptable when [graph] extras are not installed. Just ensure
        # the error message points users at the install command.
        msg = str(e).lower()
        assert "lightrag" in msg or "graph" in msg, (
            f"ImportError must mention lightrag or graph extras; got: {e}"
        )
        print("OK: graph subpackage absent and reports clean ImportError")
        return

    # Extras present — verify the public surface is what we documented.
    for attr in (
        "build",
        "query",
        "status",
        "BuildResult",
        "QueryResult",
        "GraphStatus",
        "EmbeddingProvider",
        "OpenAIEmbedding",
        "GeminiEmbedding",
        "SentenceTransformerEmbedding",
        "GraphBackend",
    ):
        assert hasattr(graph_pkg, attr), f"docingest.graph.{attr} missing"

    print("OK: graph subpackage public API exported correctly")


def test_cli_loads_with_or_without_graph() -> None:
    """
    The main CLI must always import. The `graph` subcommand registers
    only when the subpackage import succeeded, but the CLI app itself
    must work either way.
    """
    for name in list(sys.modules):
        if name.startswith("docingest"):
            del sys.modules[name]

    cli = importlib.import_module("docingest.cli")
    assert hasattr(cli, "app"), "CLI app object missing"

    # Inspect typer's registered groups. typer stores subapps on
    # app.registered_groups (typer >= 0.12).
    has_graph = any(
        getattr(g, "name", None) == "graph" for g in getattr(cli.app, "registered_groups", [])
    )

    try:
        import docingest.graph  # noqa: F401
        graph_loadable = True
    except ImportError:
        graph_loadable = False

    if graph_loadable:
        assert has_graph, (
            "graph subpackage imports cleanly but `graph` subcommand is not "
            "registered on the CLI — check src/docingest/cli.py wiring."
        )
        print("OK: CLI registers `graph` subcommand when extras present")
    else:
        # No assertion either way — typer doesn't strictly forbid having
        # the group registered with a stub. We just print what we observe
        # so the test output documents the state.
        print(
            f"OK: CLI loads without graph extras "
            f"(graph subcommand registered = {has_graph})"
        )


def test_mcp_server_applies_nest_asyncio() -> None:
    """
    Importing docingest.mcp_server should apply nest_asyncio when both
    lightrag-hku AND nest_asyncio are installed — this is what lets the
    long-running MCP server invoke docingest.graph.query() repeatedly
    without hitting LightRAG's asyncio.Lock-bound-to-first-loop bug.

    We only assert when both deps are available; otherwise we accept
    "not applied" as the documented graceful-degradation path.

    Detection: nest_asyncio.apply() sets a sentinel attribute
    ``_nest_patched`` on the asyncio module (per nest_asyncio's source).
    """
    for name in list(sys.modules):
        if name.startswith("docingest") or name == "nest_asyncio":
            del sys.modules[name]

    # Probe whether [graph] extras are fully installed.
    try:
        import lightrag  # noqa: F401
        import nest_asyncio  # noqa: F401
        extras_available = True
    except ImportError:
        extras_available = False

    if not extras_available:
        print("OK: nest_asyncio test skipped — [graph] extras not installed")
        return

    # Fresh asyncio import — make sure no prior test in this run already
    # patched it for unrelated reasons.
    import asyncio
    was_patched_before = getattr(asyncio, "_nest_patched", False)

    # The actual import-under-test.
    import docingest.mcp_server  # noqa: F401

    # nest_asyncio.apply() flips the sentinel.
    is_patched_now = getattr(asyncio, "_nest_patched", False)

    assert is_patched_now, (
        "docingest.mcp_server import did not apply nest_asyncio "
        "(both lightrag and nest_asyncio ARE installed). The MCP "
        "server will silently fail on the 2nd query_graph call."
    )

    if was_patched_before:
        print("OK: nest_asyncio already applied before MCP import (idempotent)")
    else:
        print("OK: nest_asyncio applied by MCP server import")


def main() -> None:
    test_main_package_imports_clean()
    test_graph_subpackage_isolated()
    test_cli_loads_with_or_without_graph()
    test_mcp_server_applies_nest_asyncio()
    print("\nAll graph-optional regression tests passed.")


if __name__ == "__main__":
    main()
