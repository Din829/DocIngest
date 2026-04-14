# DocIngest MCP Server

Thin MCP wrapper for DocIngest — exposes document processing as tools for AI Agents.

## Architecture

```
Agent (Claude / GPT / etc.)
    ↓ MCP protocol
mcp_server.py (transport layer ONLY — ~250 lines)
    ↓ Python function calls
DocIngest Core (pipeline.py, inspect.py, refine.py, ...)
```

**mcp_server.py contains ZERO business logic.** Every tool is a thin wrapper that:
1. Receives parameters from Agent
2. Calls the corresponding DocIngest Python API
3. Returns structured result

## Install

```bash
cd DocIngest
pip install -e ".[mcp]"
```

## Available Tools

| Tool | Description | DocIngest API |
|------|------------|---------------|
| `inspect` | Pre-flight check (size, pages, cost estimate) | `inspect_files()` |
| `run` | Process documents → knowledge base | `run_pipeline()` |
| `refine` | AI-powered Markdown cleanup | `refine_files()` |
| `search_knowledge` | Keyword search in processed knowledge base | grep on sources/*.md |
| `list_knowledge` | List knowledge base contents (files, stats) | reads index.json |
| `read_source` | Read full content of a source Markdown file | reads sources/*.md |

## Running

### stdio (default — for Claude Desktop, Claude Code, etc.)

```bash
python -m docingest.mcp_server
```

### SSE (for web clients)

```bash
python -m docingest.mcp_server --transport sse
```

## Claude Desktop Configuration

Add to `claude_desktop_config.json` (open via Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "docingest": {
      "command": "python",
      "args": ["-m", "docingest.mcp_server"],
      "cwd": "/path/to/DocIngest"
    }
  }
}
```

Restart Claude Desktop after saving (fully quit, not just close window).

## Claude Code Configuration

Add to `.mcp.json` at project root (shared via git) or `~/.claude.json` (personal, all projects):

```json
{
  "mcpServers": {
    "docingest": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "docingest.mcp_server"]
    }
  }
}
```

Supports `${VAR}` env expansion for paths and secrets.

## VS Code (GitHub Copilot) Configuration

Add to `.vscode/mcp.json` in your workspace (note: key is `servers`, not `mcpServers`):

```json
{
  "servers": {
    "docingest": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "docingest.mcp_server"]
    }
  }
}
```

MCP tools appear in Copilot Agent mode (Ctrl+Alt+I).

## Tool Usage Examples

### inspect — Check before processing

```
Agent: Use docingest inspect on ["./docs/report.pdf", "./docs/slides.pptx"]
→ Returns: [{name, format, size_mb, pages, recommendation}, ...]
```

### run — Process documents

```
Agent: Use docingest run on ["./docs/"] with output_dir="./knowledge"
→ Returns: {total_files, successful, failed, total_chunks, elapsed_ms, ...}
```

### run with overrides

```
Agent: Use docingest run on ["big_report.pdf"] with config_overrides={"parsing": {"vision": {"max_pages": 100}}}
→ Processes with custom Vision page limit
```

### refine — AI cleanup with skill selection

```
Agent: Use docingest refine on ["./knowledge/sources/report.md"]
→ Default: rewrites for readability

Agent: Use docingest refine on ["./knowledge/sources/notes.md"] with skill="refine_faithful"
→ Faithful: preserves original wording, only deduplicates and reformats
```

Available skills: `refine_default` (readability-first) | `refine_faithful` (word-for-word preservation)

### search_knowledge — Find content

```
Agent: Use docingest search_knowledge with query="解約" knowledge_dir="./knowledge"
→ Returns: {matches: [{file, line, context}, ...], knowledge_map_summary}
```

### list_knowledge — Browse knowledge base

```
Agent: Use docingest list_knowledge with knowledge_dir="./knowledge"
→ Returns: full index.json (files, formats, sections, stats)
```

## Typical Agent Workflow

```
1. inspect(["./new_docs/"])          → Understand files (size, pages, cost estimate)
2. run(["./new_docs/"])              → Process (incremental — skips cached files)
3. list_knowledge("./knowledge")     → Browse knowledge base contents
4. search_knowledge("契約", "./knowledge")  → Find specific content by keyword
5. read_source("contract.md")        → Read full file content
6. refine(["./knowledge/sources/contract.md"])  → Optional: AI cleanup for humans
```

## Adding a New Tool

1. Open `src/docingest/mcp_server.py`
2. Add a `@mcp.tool` function:

```python
@mcp.tool
def my_new_tool(param1: str, param2: int = 10) -> dict:
    """Description for the Agent."""
    from .some_module import some_function
    return some_function(param1, param2)
```

3. That's it. FastMCP auto-generates the schema from type hints + docstring.

## Modifying Existing Tools

Each tool is 10-20 lines. The pattern is always:

```python
@mcp.tool
def tool_name(params...) -> return_type:
    """Docstring = Agent sees this as tool description."""
    from .core_module import core_function   # Lazy import
    config = _get_config(overrides)          # Load config
    return core_function(params, config)     # Call core API
```

Change the core API → tool automatically follows (same function signature).
Change tool parameters → only this file changes (core API unaffected).

## Config Overrides

Every tool accepts optional `config_overrides` dict. This lets the Agent
dynamically adjust behavior without touching config files:

```python
# Agent can override any config path:
run(["docs/"], config_overrides={
    "parsing": {"vision": {"max_pages": 200, "triage": {"enabled": True}}},
    "chunking": {"strategy": "heading", "max_tokens": 1024},
    "sanitize": {"enabled": True},
})
```

## Troubleshooting

### "Module not found" errors
```bash
pip install -e ".[mcp]"   # Installs fastmcp dependency
```

### API key errors
Set in `.env` at the project root, or export as environment variables:
```bash
export GEMINI_API_KEY=...
export DASHSCOPE_API_KEY=...
```

### Large file warnings
Use `inspect` first to check file sizes, then adjust `config_overrides`
if needed (e.g. increase `max_pages` for large PDFs).
