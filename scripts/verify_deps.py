"""
DocIngest dependency gate — fails the build if required deps are missing.

DocIngest's built-in `docingest doctor` only prints a table; it never exits
non-zero. This script wraps `doctor.run_doctor()` and turns missing deps into
a real exit-1 so CI / Dockerfiles / deploy scripts can use it as a gate.

It also covers five deps the built-in doctor misses:
  - poppler (system binary, required by pdf2image fast PDF→image path)
  - node/deno/bun (JS runtime, required by yt-dlp for YouTube extractors)
  - pdf2image (Python pkg, not in pyproject — installed by install scripts)
  - pyexiftool (Python pkg, not in pyproject — installed by install scripts)
  - pywebview (Python pkg, [gui] extra — the desktop GUI shell)

Usage
-----
    python scripts/verify_deps.py                 # default profile
    python scripts/verify_deps.py --minimal       # only the 14 core Python pkgs
    python scripts/verify_deps.py --strict        # everything (all extras, all tools)
    python scripts/verify_deps.py --json          # machine-readable output for CI
    python scripts/verify_deps.py --require ocr,audio
                                                  # custom: name a list of feature
                                                  # groups that must all be present

Feature groups (used by --require / --strict)
---------------------------------------------
    office     LibreOffice (Office formats → Vision)
    media      ffmpeg + ffprobe (audio / video processing)
    pdf-fast   poppler + pdf2image (fast PDF page rendering)
    url        yt-dlp + node (video URL extraction)
    ocr        onnxruntime + rapidocr (PDF OCR for scans)
    metadata   exiftool + pyexiftool (file metadata hook)
    mcp        fastmcp (MCP server)
    audio      dashscope (Qwen3-ASR)
    nlp        sudachipy (Japanese keyword extraction)
    graph      lightrag + openai + nest_asyncio (GraphRAG layer)
    detect     magika (content-based file type detection)
    gui        pywebview (desktop GUI shell)

Exit codes
----------
    0  all required deps satisfied (warnings may have been printed)
    1  at least one required dep is missing
    2  bad CLI arguments
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

# Make `import docingest` work whether the script is run from repo root or
# from anywhere else.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Extra checks not covered by docingest.doctor
# ---------------------------------------------------------------------------

def _check_python_pkg(import_name: str) -> bool:
    try:
        __import__(import_name)
        return True
    except ImportError:
        return False


def _check_binary(name: str) -> str | None:
    """Return path or None. Uses DocIngest's own finder for platform-aware lookup."""
    try:
        from docingest.utils.binary_finder import find_binary
        path = find_binary(name)
        if path:
            return path
    except Exception:
        pass
    return shutil.which(name)


def _check_poppler() -> str | None:
    """poppler ships several binaries; pdftoppm is the one pdf2image actually calls."""
    return shutil.which("pdftoppm") or shutil.which("pdftoppm.exe")


def _check_js_runtime() -> str | None:
    """yt-dlp accepts node / deno / bun. First hit wins."""
    for rt in ("node", "deno", "bun"):
        path = shutil.which(rt)
        if path:
            return path
    return None


def _check_torch_variant() -> dict[str, Any]:
    """
    Detect whether the installed torch is the CPU build or a CUDA/ROCm build.

    DocIngest does CPU inference only — docling drags torch in transitively,
    and on Linux the default PyPI wheel is the ~5.6GB CUDA build (none of which
    DocIngest uses). A CUDA torch is not "broken", but it bloats every install
    by gigabytes for zero benefit, so we surface it as a build-gate error: the
    fix is a one-line reinstall from the CPU index.

    Returns a dict:
        installed : bool   — torch importable at all
        is_cpu    : bool   — True when this is the CPU-only build
        variant   : str    — "cpu" | "cuda <ver>" | "rocm <ver>" | "unknown"

    torch.version.cuda is None on the CPU wheel and a version string ("12.1")
    on a CUDA wheel — the canonical, build-baked signal (not a runtime GPU
    probe, so it works identically on a GPU-less CI box).
    """
    try:
        import torch  # noqa: PLC0415 — intentional lazy import (heavy)
    except Exception:
        return {"installed": False, "is_cpu": False, "variant": "unknown"}

    cuda_ver = getattr(torch.version, "cuda", None)
    hip_ver = getattr(torch.version, "hip", None)
    if cuda_ver:
        return {"installed": True, "is_cpu": False, "variant": f"cuda {cuda_ver}"}
    if hip_ver:
        return {"installed": True, "is_cpu": False, "variant": f"rocm {hip_ver}"}
    return {"installed": True, "is_cpu": True, "variant": "cpu"}


# ---------------------------------------------------------------------------
# Install hints (platform-aware)
# ---------------------------------------------------------------------------

def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


_HINTS: dict[str, dict[str, str]] = {
    "poppler": {
        "win32": "Download from https://github.com/oschwartz10612/poppler-windows/releases/latest "
                 "and add bin\\ to PATH (or run scripts\\install_system_deps.ps1)",
        "darwin": "brew install poppler",
        "linux": "sudo apt install poppler-utils  # or: sudo dnf install poppler-utils",
    },
    "js_runtime": {
        "win32": "winget install OpenJS.NodeJS",
        "darwin": "brew install node",
        "linux": "sudo apt install nodejs  # or: sudo dnf install nodejs",
    },
    "onnxruntime": {
        "win32": "pip install onnxruntime",
        "darwin": "pip install onnxruntime",
        "linux": "pip install onnxruntime",
    },
    "rapidocr": {
        # docling 2.51+ uses the unified `rapidocr` package, NOT the legacy
        # `rapidocr-onnxruntime`. Installing the wrong name → silent OCR
        # failure on docling >=2.51. See docling/discussions/2249.
        "win32": "pip install rapidocr",
        "darwin": "pip install rapidocr",
        "linux": "pip install rapidocr",
    },
    "pdf2image": {
        "win32": "pip install pdf2image",
        "darwin": "pip install pdf2image",
        "linux": "pip install pdf2image",
    },
    "pyexiftool": {
        "win32": "pip install PyExifTool",
        "darwin": "pip install PyExifTool",
        "linux": "pip install PyExifTool",
    },
    "pywebview": {
        "win32": 'pip install "pywebview>=6.2,<7"  # or: pip install -e ".[gui]"',
        "darwin": 'pip install "pywebview>=6.2,<7"  # or: pip install -e ".[gui]"',
        "linux": 'pip install "pywebview>=6.2,<7"  # or: pip install -e ".[gui]"',
    },
}


# ---------------------------------------------------------------------------
# Feature groups → list of (kind, name, check_fn)
# kind:   "core"   missing core pkg, mapped from doctor.core
#         "tool"   system binary, mapped from doctor.tools or _check_binary
#         "opt"    optional pkg, mapped from doctor.optional
#         "extra"  not in doctor at all, checked here
# ---------------------------------------------------------------------------

_GROUPS: dict[str, list[tuple[str, str]]] = {
    "office":   [("tool", "LibreOffice")],
    "media":    [("tool", "ffmpeg"), ("extra", "ffprobe")],
    "pdf-fast": [("extra", "poppler"), ("extra", "pdf2image")],
    "url":      [("opt", "yt-dlp"), ("extra", "js_runtime")],
    "ocr":      [("extra", "onnxruntime"), ("extra", "rapidocr")],
    "metadata": [("tool", "ExifTool"), ("extra", "pyexiftool")],
    "mcp":      [("opt", "fastmcp")],
    "audio":    [("opt", "dashscope")],
    "nlp":      [("opt", "sudachipy")],
    "graph":    [("opt", "lightrag-hku"), ("opt", "nest_asyncio")],
    "detect":   [("opt", "magika")],
    "gui":      [("extra", "pywebview")],
}

# Default profile: what must be present when no --require / --strict given.
# Picked to match "typical RAG/Agentic ingest" use case:
#   - all core Python pkgs (mandatory, always)
#   - LibreOffice (Office formats are common)
#   - ffmpeg (media support is common)
# Everything else is opt-in.
_DEFAULT_REQUIRED = ["office", "media"]


# ---------------------------------------------------------------------------
# Main collection
# ---------------------------------------------------------------------------

def _collect_status() -> dict[str, Any]:
    """Run doctor + extra checks. Return one merged status dict."""
    from docingest.doctor import run_doctor
    doctor = run_doctor({})

    # Extras: things doctor doesn't see
    extras = {
        "poppler": {"ok": _check_poppler() is not None, "path": _check_poppler()},
        "js_runtime": {"ok": _check_js_runtime() is not None, "path": _check_js_runtime()},
        "onnxruntime": {"ok": _check_python_pkg("onnxruntime")},
        # Accept either the new `rapidocr` (3.x, what docling >=2.51 needs) OR
        # the legacy `rapidocr-onnxruntime` (1.x, what docling <2.51 used). Old
        # installs still register as "ok" so we don't false-alarm. Install hint
        # above always recommends the new name.
        "rapidocr": {"ok": _check_python_pkg("rapidocr") or _check_python_pkg("rapidocr_onnxruntime")},
        "pdf2image": {"ok": _check_python_pkg("pdf2image")},
        "pyexiftool": {"ok": _check_python_pkg("exiftool")},
        "pywebview": {"ok": _check_python_pkg("webview")},
        "ffprobe": {"ok": _check_binary("ffprobe") is not None, "path": _check_binary("ffprobe")},
    }

    return {"doctor": doctor, "extras": extras, "torch": _check_torch_variant()}


def _is_ok(status: dict[str, Any], kind: str, name: str) -> bool:
    if kind == "core":
        return status["doctor"]["core"].get(name, {}).get("ok", False)
    if kind == "tool":
        return status["doctor"]["tools"].get(name, {}).get("ok", False)
    if kind == "opt":
        return status["doctor"]["optional"].get(name, {}).get("ok", False)
    if kind == "extra":
        return status["extras"].get(name, {}).get("ok", False)
    return False


def _hint(status: dict[str, Any], kind: str, name: str) -> str:
    plat = _platform_key()
    if kind == "core":
        return status["doctor"]["core"].get(name, {}).get("hint") or f"pip install {name}"
    if kind == "tool":
        return status["doctor"]["tools"].get(name, {}).get("hint") or ""
    if kind == "opt":
        return status["doctor"]["optional"].get(name, {}).get("hint") or f"pip install {name}"
    if kind == "extra":
        return _HINTS.get(name, {}).get(plat, "")
    return ""


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _evaluate(
    status: dict[str, Any],
    required_groups: list[str],
    check_optional: bool,
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) — each a list of human-readable lines."""
    errors: list[str] = []
    warnings: list[str] = []

    # Python version + all 14 core pkgs are ALWAYS required.
    py = status["doctor"]["python"]
    if not py["ok"]:
        errors.append(f"Python {py['version']} is too old. {py['hint']}")

    for name, info in status["doctor"]["core"].items():
        if not info["ok"]:
            errors.append(f"core pkg [{name}] missing → {info['hint'] or 'pip install ' + name}")

    # torch variant — ALWAYS checked (independent of profile). DocIngest is
    # CPU-only; a CUDA/ROCm torch bloats the install by ~5GB for zero benefit.
    # Treated as an error so the build gate refuses a wrong-variant install and
    # prints the one-line fix. torch missing entirely → silent (docling pulls
    # it transitively, so a real install always has it; if it's absent the core
    # pkg check above already fires).
    torch_info = status.get("torch", {})
    if torch_info.get("installed") and not torch_info.get("is_cpu"):
        errors.append(
            f"torch is the {torch_info.get('variant', 'non-CPU')} build "
            f"(~5GB, DocIngest uses CPU inference only) → reinstall the CPU "
            f"wheel: pip install torch torchvision "
            f"--index-url https://download.pytorch.org/whl/cpu --force-reinstall"
        )

    # Required groups → errors if missing
    for group in required_groups:
        items = _GROUPS.get(group)
        if items is None:
            warnings.append(f"unknown feature group '{group}' (ignored)")
            continue
        for kind, name in items:
            if not _is_ok(status, kind, name):
                hint = _hint(status, kind, name)
                errors.append(f"[{group}] {name} missing → {hint}")

    # Non-required groups → warnings if missing (only when --check-optional)
    if check_optional:
        for group, items in _GROUPS.items():
            if group in required_groups:
                continue
            for kind, name in items:
                if not _is_ok(status, kind, name):
                    hint = _hint(status, kind, name)
                    warnings.append(f"[{group}] {name} not installed → {hint}")

    # API key warning — Vision is broken without at least one of these.
    keys = status["doctor"]["api_keys"]
    if not keys["GEMINI_API_KEY"]["set"] and not keys["OPENAI_API_KEY"]["set"]:
        warnings.append(
            "No Vision API key set (GEMINI_API_KEY or OPENAI_API_KEY). "
            "Set in .env or environment before running Vision-enabled ingest."
        )

    return errors, warnings


def _print_text(
    required_groups: list[str],
    errors: list[str],
    warnings: list[str],
) -> None:
    print("=" * 60)
    print(f"DocIngest dependency check (required: {', '.join(required_groups) or 'core only'})")
    print("=" * 60)

    if warnings:
        print(f"\n⚠️  {len(warnings)} warning(s):")
        for w in warnings:
            print(f"   - {w}")

    if errors:
        print(f"\n❌ {len(errors)} required dep(s) MISSING:")
        for e in errors:
            print(f"   - {e}")
        print("\nFix the above, then re-run. See scripts/README.md for one-shot installers.")
    else:
        print("\n✅ All required dependencies satisfied.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DocIngest dependency gate — exits non-zero when required deps are missing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--minimal", action="store_true",
                        help="Only check the 14 core Python packages. No tools, no extras.")
    parser.add_argument("--strict", action="store_true",
                        help="Require EVERY feature group to be present (most strict).")
    parser.add_argument("--require", default="",
                        help="Comma-separated feature groups that must be present. "
                             f"Available: {','.join(_GROUPS)}.")
    parser.add_argument("--json", action="store_true",
                        help="Emit raw JSON status to stdout (for CI consumption).")
    parser.add_argument("--no-warnings", action="store_true",
                        help="Suppress the warnings section (errors still shown).")
    args = parser.parse_args(argv)

    # Resolve which groups are "required"
    if args.minimal:
        required = []
    elif args.strict:
        required = list(_GROUPS.keys())
    elif args.require:
        required = [g.strip() for g in args.require.split(",") if g.strip()]
    else:
        required = list(_DEFAULT_REQUIRED)

    # Validate group names
    unknown = [g for g in required if g not in _GROUPS]
    if unknown:
        print(f"error: unknown feature group(s): {unknown}", file=sys.stderr)
        print(f"available: {','.join(_GROUPS)}", file=sys.stderr)
        return 2

    try:
        status = _collect_status()
    except ImportError as e:
        print(f"error: cannot import docingest ({e}). Run from repo root or "
              f"`pip install -e .` first.", file=sys.stderr)
        return 1

    # In --minimal mode, skip optional warnings too (the user explicitly said
    # they don't care about anything beyond core).
    check_optional = not args.minimal and not args.no_warnings
    errors, warnings = _evaluate(status, required, check_optional)

    if args.json:
        payload = {
            "required_groups": required,
            "ok": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "status": status,
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_text(required, errors, warnings if not args.no_warnings else [])

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
