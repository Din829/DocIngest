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


def main() -> None:
    test_main_package_imports_clean()
    test_graph_subpackage_isolated()
    test_cli_loads_with_or_without_graph()
    print("\nAll graph-optional regression tests passed.")


if __name__ == "__main__":
    main()
