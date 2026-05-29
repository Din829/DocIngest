"""
Anti-drift guard for the agent-facing command catalog.

The "command catalog" — a single table duplicated (by design, same content) in
.claude/skills/docingest/SKILL.md and AGENTS.md — is how an agent learns every
DocIngest command at a glance. Hand-written tables drift from code over time
(that's how README's --strategy list and the MCP refine default both went stale).

This test pins the table to the CODE: it pulls the real command set, the real
--strategy values, and the real refine default from source, then asserts the
catalog documents reflect them. Change a command but forget the table → red.

Run:
    python tests/unit/test_command_catalog.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

SKILL_MD = ROOT / ".claude" / "skills" / "docingest" / "SKILL.md"
AGENTS_MD = ROOT / "AGENTS.md"
DEFAULT_YAML = ROOT / "config" / "default.yaml"


def _real_command_set() -> tuple[set[str], set[str], set[str]]:
    """(cli_commands, graph_subcommands, mcp_tools) straight from the code."""
    from docingest.cli import app
    from docingest.graph.cli import graph_app

    cli_cmds = {c.name for c in app.registered_commands if c.name}
    graph_cmds = {c.name for c in graph_app.registered_commands if c.name}

    # MCP tools = the @mcp.tool functions. The graph ones are registered inside
    # an `if _GRAPH_AVAILABLE:` block, so we read the source rather than import
    # (importing fastmcp may not be installed in every test env).
    mcp_src = (ROOT / "src" / "docingest" / "mcp_server.py").read_text(encoding="utf-8")
    mcp_tools = set(re.findall(r"@mcp\.tool\s+def\s+(\w+)", mcp_src))
    return cli_cmds, graph_cmds, mcp_tools


def test_skill_and_agents_list_every_command():
    """Every real command/subcommand/tool name must appear in BOTH catalog docs."""
    print("=== test_skill_and_agents_list_every_command ===")
    cli_cmds, graph_cmds, mcp_tools = _real_command_set()
    all_names = cli_cmds | graph_cmds | mcp_tools
    print(f"  code commands: cli={sorted(cli_cmds)} graph={sorted(graph_cmds)} "
          f"mcp={sorted(mcp_tools)}")

    skill_text = SKILL_MD.read_text(encoding="utf-8")
    agents_text = AGENTS_MD.read_text(encoding="utf-8")

    for doc_name, text in [("SKILL.md", skill_text), ("AGENTS.md", agents_text)]:
        missing = sorted(n for n in all_names if n not in text)
        assert not missing, (
            f"{doc_name} command catalog is missing these real commands/tools: "
            f"{missing}. Update the table to match the code."
        )
    print(f"  all {len(all_names)} command names present in both docs  PASSED\n")


def test_strategy_values_match_code():
    """The --strategy values in the catalog must match what cli.py actually accepts."""
    print("=== test_strategy_values_match_code ===")
    cli_src = (ROOT / "src" / "docingest" / "cli.py").read_text(encoding="utf-8")
    # cli.py help: "Override chunking strategy: auto, heading, recursive, slide, sheet."
    m = re.search(r"Override chunking strategy:\s*([a-z, ]+)", cli_src)
    assert m, "could not find the --strategy help line in cli.py"
    real_values = {v.strip() for v in m.group(1).split(",") if v.strip()}
    print(f"  code --strategy values: {sorted(real_values)}")

    # Both catalog docs spell them as auto|heading|recursive|slide|sheet
    for doc_name, path in [("SKILL.md", SKILL_MD), ("AGENTS.md", AGENTS_MD)]:
        text = path.read_text(encoding="utf-8")
        missing = sorted(v for v in real_values if v not in text)
        assert not missing, (
            f"{doc_name} is missing --strategy value(s) {missing}; "
            f"code accepts {sorted(real_values)}."
        )
    print("  all strategy values present in both docs  PASSED\n")


def test_refine_default_not_stale():
    """The refine.max_input_tokens default in docs must equal default.yaml (not the old 8000)."""
    print("=== test_refine_default_not_stale ===")
    yaml_text = DEFAULT_YAML.read_text(encoding="utf-8")
    m = re.search(r"max_input_tokens:\s*(\d+)", yaml_text)
    assert m, "could not find refine.max_input_tokens in default.yaml"
    real_default = m.group(1)
    print(f"  default.yaml refine.max_input_tokens = {real_default}")
    assert real_default != "8000", "unexpected: yaml itself says 8000?"

    # The MCP refine docstring states the default explicitly; it must match
    # and must NOT carry the stale 8000.
    mcp_text = (ROOT / "src" / "docingest" / "mcp_server.py").read_text(encoding="utf-8")
    m2 = re.search(r"refine\.max_input_tokens\s*\(default\s*(\d+)\)", mcp_text)
    assert m2, "could not find 'refine.max_input_tokens (default N)' in mcp_server.py"
    assert m2.group(1) == real_default, (
        f"mcp_server.py says default {m2.group(1)} but default.yaml is {real_default}"
    )
    print(f"  mcp docstring default matches yaml ({real_default})  PASSED\n")


def main():
    test_skill_and_agents_list_every_command()
    test_strategy_values_match_code()
    test_refine_default_not_stale()
    print("ALL command-catalog anti-drift TESTS PASSED")


if __name__ == "__main__":
    main()
