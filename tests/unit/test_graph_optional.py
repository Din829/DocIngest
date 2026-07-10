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

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _run_clean(code: str) -> None:
    """Run an import-isolation assertion without mutating this pytest process."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_main_package_imports_clean() -> None:
    """The main facade must import without touching the graph subpackage."""
    _run_clean("""
import sys
import docingest
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
assert "docingest.graph" not in sys.modules, (
        "docingest.graph was loaded as a side-effect of `import docingest` — "
        "the optional layer must require explicit `import docingest.graph`."
)
""")

    print("OK: docingest imports without graph subpackage")


def test_graph_subpackage_isolated() -> None:
    """
    Importing docingest.graph must not error when [graph] extras are
    installed, and must raise a clean ImportError otherwise.
    """
    _run_clean("""
try:
    import docingest.graph as graph_pkg
except ImportError as e:
    msg = str(e).lower()
    assert "lightrag" in msg or "graph" in msg, (
        f"ImportError must mention lightrag or graph extras; got: {e}"
    )
else:
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
""")
    print("OK: graph subpackage public API exported correctly")


def test_cli_loads_with_or_without_graph() -> None:
    """
    The main CLI must always import. The `graph` subcommand registers
    only when the subpackage import succeeded, but the CLI app itself
    must work either way.
    """
    _run_clean("""
import importlib
cli = importlib.import_module("docingest.cli")
assert hasattr(cli, "app"), "CLI app object missing"
has_graph = any(
        getattr(g, "name", None) == "graph" for g in getattr(cli.app, "registered_groups", [])
)
try:
    import docingest.graph
    graph_loadable = True
except ImportError:
    graph_loadable = False
if graph_loadable:
    assert has_graph, (
            "graph subpackage imports cleanly but `graph` subcommand is not "
            "registered on the CLI — check src/docingest/cli.py wiring."
    )
""")
    print("OK: CLI loads with graph optionality preserved")


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
    _run_clean("""
try:
    import lightrag
    import nest_asyncio
except ImportError:
    raise SystemExit(0)
import asyncio
import docingest.mcp_server
assert getattr(asyncio, "_nest_patched", False), (
        "docingest.mcp_server import did not apply nest_asyncio "
        "(both lightrag and nest_asyncio ARE installed). The MCP "
        "server will silently fail on the 2nd query_graph call."
)
""")
    print("OK: nest_asyncio optional path verified in a clean process")


def main() -> None:
    test_main_package_imports_clean()
    test_graph_subpackage_isolated()
    test_cli_loads_with_or_without_graph()
    test_mcp_server_applies_nest_asyncio()
    print("\nAll graph-optional regression tests passed.")


if __name__ == "__main__":
    main()
