"""PyInstaller entry point for the DocIngest GUI exe.

Two modes:
  DocIngest.exe                       — launch the desktop GUI (the normal path)
  DocIngest.exe --smoke SAMPLES OUT   — headless self-test: verify every
                                        bundled dependency actually works
                                        inside the frozen bundle. Used by
                                        packaging/smoke_test.py as the build
                                        gate; also handy for support ("run
                                        this and send me the output").

The smoke runner lives HERE (not in smoke_test.py) because it must execute
*inside* the frozen bundle — that is the whole point: an import that works on
the build machine but wasn't collected into the exe only fails in here.
"""
from __future__ import annotations

import sys


# Modules whose absence in the bundle would silently degrade or break a core
# feature. Each is imported by name inside the frozen exe — a PyInstaller
# collection miss turns into a loud FAIL line instead of a runtime surprise.
_CRITICAL_IMPORTS = [
    "docling.document_converter",
    "docling_core",
    "docling_parse",
    "docling_ibm_models",
    "litellm",
    "google.genai",
    "magika",
    "rapidocr",
    "onnxruntime",
    "openpyxl",
    "pptx",
    "docx",
    "fitz",                    # pymupdf
    "PIL",
    "yaml",
    "diskcache",
    "olefile",
    "bs4",
    "defusedxml",
    "webview",                 # pywebview (GUI shell)
    "dashscope",               # audio ASR
    "sudachipy",               # Japanese keywords
    "lightrag",                # graph build (GUI exposes it)
    "nest_asyncio",
    "yt_dlp",                  # URL ingestion
    "pdf2image",
    "exiftool",                # PyExifTool
]


def _smoke(samples_dir: str, out_dir: str) -> int:
    """Run the in-bundle dependency matrix. Returns a process exit code.

    All output goes to <out_dir>/smoke.log — the shipped exe is windowed
    (console=False) so it HAS no usable stdout; the host driver
    (packaging/smoke_test.py) reads and relays the log."""
    import os
    import traceback
    from pathlib import Path

    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    log = open(out_root / "smoke.log", "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log

    failures: list[str] = []

    def check(label: str, fn) -> None:
        try:
            fn()
            print(f"  OK   {label}")
        except Exception as e:
            failures.append(f"{label}: {e}")
            print(f"  FAIL {label}: {e}")
            traceback.print_exc()

    print("=== DocIngest exe smoke test ===")
    print(f"frozen: {getattr(sys, 'frozen', False)}  base: {getattr(sys, '_MEIPASS', '-')}")

    # 1. Bundled-path injection. In a frozen bundle these MUST resolve —
    #    a missing one means the packaging step forgot an asset.
    from docingest.utils.bundled_binaries import ensure_bundled_binaries
    injected = ensure_bundled_binaries()
    print(f"  injected: {sorted(injected)}")
    if getattr(sys, "frozen", False):
        for var in ("SOFFICE_PATH", "FFMPEG_PATH", "FFPROBE_PATH", "DOCLING_ARTIFACTS_PATH"):
            check(f"env {var}", lambda v=var: (_ for _ in ()).throw(
                RuntimeError("not set / not a real path")
            ) if not (os.environ.get(v) and Path(os.environ[v]).exists()) else None)

    # 2. Critical imports — inside the bundle.
    import importlib
    for mod in _CRITICAL_IMPORTS:
        check(f"import {mod}", lambda m=mod: importlib.import_module(m))

    # 3. Frontend assets shipped next to the gui module.
    check("gui web assets", lambda: _assert_web_assets())

    # 4. Real end-to-end parse of every sample file, Vision disabled so the
    #    whole matrix runs OFFLINE (docling models + soffice + denoise +
    #    chunking all execute for real; only the cloud LLM step is off).
    samples = Path(samples_dir)
    out = Path(out_dir)
    if samples.is_dir() and any(samples.iterdir()):
        import docingest

        def run_ingest():
            result = docingest.ingest(
                str(samples),
                output=str(out),
                config_overrides={
                    "parsing.vision.enabled": False,
                    "incremental.enabled": False,
                },
            )
            ok, total = result.stats["successful"], result.stats["total_files"]
            if ok != total or total == 0:
                raise RuntimeError(
                    f"{ok}/{total} files parsed; errors={result.stats['errors']}"
                )
            if not result.chunks:
                raise RuntimeError("no chunks produced")
            print(f"       ({ok}/{total} files, {len(result.chunks)} chunks)")

        check("ingest sample matrix", run_ingest)
    else:
        print(f"  SKIP ingest matrix (no samples at {samples})")

    # 5. doctor must at least run (it is the user-facing self-check).
    def run_doctor():
        from docingest.doctor import run_doctor
        report = run_doctor({}, fast=True)
        if not report["tools"]["LibreOffice"]["ok"] and getattr(sys, "frozen", False):
            raise RuntimeError("doctor cannot see bundled LibreOffice")

    check("doctor", run_doctor)

    print("=" * 34)
    if failures:
        print(f"SMOKE FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SMOKE PASSED")
    return 0


def _assert_web_assets() -> None:
    from pathlib import Path
    import docingest.gui.gui_app as g
    index = g._index_html_path()
    if not index.is_file():
        raise RuntimeError(f"index.html missing at {index}")
    app_js = index.parent / "app.js"
    if not app_js.is_file():
        raise RuntimeError("app.js missing next to index.html")


def main() -> None:
    if "--smoke" in sys.argv:
        i = sys.argv.index("--smoke")
        try:
            samples, out = sys.argv[i + 1], sys.argv[i + 2]
        except IndexError:
            print("usage: DocIngest.exe --smoke <samples_dir> <out_dir>")
            sys.exit(2)
        sys.exit(_smoke(samples, out))

    from docingest.gui.gui_app import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
