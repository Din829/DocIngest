"""
Environment health check — verifies all dependencies are available.

Usage:
    docingest doctor

Reports: Python version, core packages, optional packages, external tools,
and API keys. Tells you exactly what's missing and how to fix it.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any


def _check_import(module: str) -> tuple[str | None, str | None]:
    """Try importing a module. Returns (version, None) or (None, error)."""
    try:
        mod = __import__(module)
        version = getattr(mod, "__version__", getattr(mod, "VERSION", "installed"))
        return str(version), None
    except ImportError as e:
        return None, str(e)


def _check_binary(name: str, config: dict[str, Any] | None = None) -> str | None:
    """Find external binary. Returns path or None."""
    try:
        from .utils.binary_finder import find_binary
        return find_binary(name, config)
    except Exception:
        return shutil.which(name)


# Switches that, when ON, cause at least one extra LLM/Vision API call per
# pipeline run. Surfaced by `docingest doctor` so users see exactly which
# config knobs are currently incurring AI cost. Data-driven: adding a new
# cost-incurring switch is one entry here, no code change needed.
#
# Each entry: (config_path, when_on_summary, scope)
#   scope: "per-page"  — fires N times where N = page count after triage
#          "per-chunk" — fires once per chunk
#          "per-run"   — fires once per ingest() call (cheap-ish)
_COST_SWITCHES: list[tuple[str, str, str]] = [
    ("parsing.vision.enabled",
     "Vision API call per page (after triage)", "per-page"),
    ("knowledge_map.enabled",
     "Stage 1 keyword extraction (zero cost) + Stage 2 LLM summary "
     "if ai_summary=true", "per-run"),
    ("knowledge_map.ai_summary",
     "LLM-generated knowledge base summary + search guide", "per-run"),
    ("chunking.enrichment.contextual_summary",
     "LLM-generated per-chunk summary (rarely needed)", "per-chunk"),
    ("chunking.ai_assist.enabled",
     "LLM-assisted chunk boundary refinement on oversized chunks", "per-chunk"),
    ("metadata.exiftool.enabled",
     "exiftool subprocess (no LLM cost, but external binary)", "per-file"),
]


def run_doctor(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Run full environment check. Returns structured results.

    Can be called programmatically (returns dict) or via CLI (prints table).
    """
    results: dict[str, Any] = {
        "python": {}, "core": {}, "optional": {}, "tools": {}, "api_keys": {},
        "cost_switches": [],
    }

    # --- Python ---
    results["python"] = {
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "ok": sys.version_info >= (3, 10),
        "hint": "Requires Python >= 3.10" if sys.version_info < (3, 10) else None,
    }

    # --- Core packages ---
    core_packages = {
        "docling": "docling",
        "litellm": "litellm",
        "typer": "typer",
        "pyyaml": "yaml",
        "diskcache": "diskcache",
        "rich": "rich",
        "python-dotenv": "dotenv",
        "openpyxl": "openpyxl",
        "pillow": "PIL",
        "python-pptx": "pptx",
        "beautifulsoup4": "bs4",
        "defusedxml": "defusedxml",
        "pymupdf": "pymupdf",
        "requests": "requests",
    }
    for pkg_name, import_name in core_packages.items():
        ver, err = _check_import(import_name)
        results["core"][pkg_name] = {
            "version": ver,
            "ok": ver is not None,
            "hint": f"pip install {pkg_name}" if err else None,
        }

    # --- Optional packages ---
    optional_packages = {
        "sudachipy": {"import": "sudachipy", "install": 'pip install -e ".[nlp]"', "purpose": "Japanese keyword extraction"},
        "fastmcp": {"import": "fastmcp", "install": 'pip install -e ".[mcp]"', "purpose": "MCP Server"},
        "dashscope": {"import": "dashscope", "install": 'pip install -e ".[audio]"', "purpose": "Audio transcription (Qwen3-ASR)"},
        "magika": {"import": "magika", "install": "pip install magika", "purpose": "Content-based file type detection"},
        "yt-dlp": {"import": "yt_dlp", "install": "pip install yt-dlp", "purpose": "YouTube/Bilibili URL support"},
        "lightrag-hku": {"import": "lightrag", "install": 'pip install -e ".[graph]"', "purpose": "GraphRAG layer (entity / relation / community graph + queries)"},
        "nest_asyncio": {"import": "nest_asyncio", "install": 'pip install -e ".[graph]"', "purpose": "Enables repeated graph.query() in MCP server (auto-applied there)"},
        "sentence-transformers": {"import": "sentence_transformers", "install": 'pip install -e ".[graph-local]"', "purpose": "Local embeddings for GraphRAG (zero API cost)"},
    }
    for pkg_name, info in optional_packages.items():
        ver, err = _check_import(info["import"])
        results["optional"][pkg_name] = {
            "version": ver,
            "ok": ver is not None,
            "purpose": info["purpose"],
            "hint": info["install"] if err else None,
        }

    # --- External tools ---
    tools = {
        "LibreOffice": {"binary": "soffice", "install_hint": {
            "win32": "winget install TheDocumentFoundation.LibreOffice",
            "darwin": "brew install --cask libreoffice",
            "linux": "sudo apt install libreoffice",
        }, "purpose": "Vision enrichment for Excel/Word/PPT"},
        "ffmpeg": {"binary": "ffmpeg", "install_hint": {
            "win32": "winget install Gyan.FFmpeg",
            "darwin": "brew install ffmpeg",
            "linux": "sudo apt install ffmpeg",
        }, "purpose": "Audio extraction + segmentation"},
        "ExifTool": {"binary": "exiftool", "install_hint": {
            "win32": "winget install OliverBetz.ExifTool",
            "darwin": "brew install exiftool",
            "linux": "sudo apt install exiftool",
        }, "purpose": "File metadata extraction (optional)"},
    }
    platform = "win32" if sys.platform.startswith("win") else ("darwin" if sys.platform == "darwin" else "linux")
    for tool_name, info in tools.items():
        path = _check_binary(info["binary"], config)
        results["tools"][tool_name] = {
            "path": path,
            "ok": path is not None,
            "purpose": info["purpose"],
            "hint": info["install_hint"].get(platform, "") if not path else None,
        }

    # --- API Keys ---
    # Load .env if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    api_keys = {
        "GEMINI_API_KEY": {"purpose": "Vision AI (default primary)", "required": "If using Vision"},
        "DASHSCOPE_API_KEY": {"purpose": "Audio transcription (Qwen3-ASR)", "required": "If using audio"},
        "OPENAI_API_KEY": {"purpose": "Vision/ASR fallback", "required": "Optional"},
    }
    for key_name, info in api_keys.items():
        value = os.environ.get(key_name, "")
        results["api_keys"][key_name] = {
            "set": bool(value),
            "purpose": info["purpose"],
            "required": info["required"],
        }

    # --- Cost-incurring switches ---
    # Surfaces which config knobs WILL trigger LLM/external calls on the next
    # ingest() run. Reads from the live merged config (default.yaml + project
    # YAML + env overrides + any CLI overrides the caller passed in), so the
    # values shown match what an actual run will see.
    if config is not None:
        from .config import get_nested
        for path, when_on, scope in _COST_SWITCHES:
            value = get_nested(config, path, None)
            results["cost_switches"].append({
                "path": path,
                "enabled": bool(value),
                "when_on": when_on,
                "scope": scope,
            })

    return results


def print_doctor(results: dict[str, Any]) -> None:
    """Pretty-print doctor results using Rich."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print("\n[bold]=== DocIngest Environment Check ===[/bold]\n")

    # Python
    py = results["python"]
    status = "[green]OK[/green]" if py["ok"] else f"[red]NG[/red] ({py['hint']})"
    console.print(f"  Python: {py['version']} {status}\n")

    # Core packages
    table = Table(title="Core Packages")
    table.add_column("Package", style="bold")
    table.add_column("Version", justify="right")
    table.add_column("Status")
    for name, info in results["core"].items():
        if info["ok"]:
            table.add_row(name, info["version"], "[green]OK[/green]")
        else:
            table.add_row(name, "—", f"[red]MISSING[/red]  {info['hint']}")
    console.print(table)

    # Optional packages
    table2 = Table(title="Optional Packages")
    table2.add_column("Package", style="bold")
    table2.add_column("Version", justify="right")
    table2.add_column("Purpose")
    table2.add_column("Status")
    for name, info in results["optional"].items():
        if info["ok"]:
            table2.add_row(name, info["version"], info["purpose"], "[green]OK[/green]")
        else:
            table2.add_row(name, "—", info["purpose"], f"[yellow]not installed[/yellow]  {info['hint']}")
    console.print(table2)

    # External tools
    table3 = Table(title="External Tools")
    table3.add_column("Tool", style="bold")
    table3.add_column("Path")
    table3.add_column("Purpose")
    table3.add_column("Status")
    for name, info in results["tools"].items():
        if info["ok"]:
            table3.add_row(name, info["path"], info["purpose"], "[green]OK[/green]")
        else:
            table3.add_row(name, "—", info["purpose"], f"[yellow]not found[/yellow]  {info['hint']}")
    console.print(table3)

    # API Keys
    table4 = Table(title="API Keys (.env)")
    table4.add_column("Key", style="bold")
    table4.add_column("Purpose")
    table4.add_column("Required")
    table4.add_column("Status")
    for name, info in results["api_keys"].items():
        if info["set"]:
            table4.add_row(name, info["purpose"], info["required"], "[green]set[/green]")
        else:
            table4.add_row(name, info["purpose"], info["required"], "[yellow]not set[/yellow]")
    console.print(table4)

    # Cost-incurring switches — show which knobs WILL trigger LLM / external
    # calls on the next ingest() run. Helps catch surprises like "Vision is
    # off but knowledge_map.ai_summary still burns one LLM call per run".
    switches = results.get("cost_switches") or []
    if switches:
        table5 = Table(title="LLM / Cost-incurring Switches")
        table5.add_column("Config path", style="bold")
        table5.add_column("State")
        table5.add_column("Scope", justify="center")
        table5.add_column("When ON")
        for sw in switches:
            state = "[yellow]ON[/yellow]" if sw["enabled"] else "[green]off[/green]"
            table5.add_row(sw["path"], state, sw["scope"], sw["when_on"])
        console.print(table5)

    # Summary
    core_ok = all(v["ok"] for v in results["core"].values())
    if core_ok:
        console.print("\n[green]Core dependencies OK.[/green] Run `docingest run` to process documents.\n")
    else:
        missing = [k for k, v in results["core"].items() if not v["ok"]]
        console.print(f"\n[red]Missing core packages: {', '.join(missing)}[/red]")
        console.print("Fix: [bold]pip install -e .[/bold]\n")
