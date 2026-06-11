# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the DocIngest desktop GUI (Windows, onedir).
#
# Hard rules (performance contract — see docs/GUI/GUI_DESIGN.md packaging
# section):
#   * onedir, NEVER onefile  — onefile re-extracts ~2GB on every launch.
#   * upx=False              — runtime decompression cost + AV false positives.
#   * console=False          — desktop app; errors surface via the WebView2
#                              message box in gui_app.main.
#
# Iteration contract (source edits → fast repackage): the ~1.7GB of system
# binaries and ML models are NOT spec datas. PyInstaller here builds ONLY the
# Python side into build/pyi_dist; build_exe.ps1's assemble stage then
# robocopy-MIRRORs that plus build/bins → _internal/_bundled_bin and
# build/models → _internal/_bundled_models into the final dist (incremental:
# unchanged files are skipped, so a code-only rebuild never re-copies
# LibreOffice). Runtime layout is identical to shipping them as datas —
# ensure_bundled_binaries() looks under sys._MEIPASS (= _internal in onedir).

import os
from PyInstaller.utils.hooks import collect_all

SPEC_DIR = os.path.abspath(SPECPATH)
ROOT = os.path.dirname(SPEC_DIR)
BUILD = os.path.join(SPEC_DIR, "build")

datas = [
    # Frontend + bundled config/skills. Targets mirror the source layout the
    # code resolves at runtime (gui_app: Path(__file__).parent/'web';
    # config.py / refine.py: _MEIPASS/config, _MEIPASS/skills).
    (os.path.join(ROOT, "src", "docingest", "gui", "web"), os.path.join("docingest", "gui", "web")),
    (os.path.join(ROOT, "config", "default.yaml"), "config"),
    (os.path.join(ROOT, "skills"), "skills"),
]
binaries = []
hiddenimports = [
    # docingest's own lazy/dynamic imports that static analysis can miss.
    "docingest.gui.gui_app",
    "docingest.parsers.media_parser",
    "docingest.graph",
    "exiftool",
    "pdf2image",
    "nest_asyncio",
    # DLL-heavy packages handled by PyInstaller's OFFICIAL hooks — listed
    # here only so analysis reaches them; do NOT collect_all these (see below).
    "torch",
    "torchvision",
    "onnxruntime",
]

# Dynamic-import heavy packages: collect EVERYTHING (modules, datas, dlls).
# Deliberately greedy — a few hundred MB of maybe-unused submodules is the
# price of "an import miss can only happen for code we never ship".
#
# HARD RULE (learned from a failed smoke gate): packages with an OFFICIAL
# PyInstaller hook that manages native-DLL layout — torch, torchvision,
# onnxruntime, transformers — must NOT be collect_all'd. Doing so copies
# their DLLs to a second location and the duplicate load fails with
# [WinError 1114] (c10.dll / onnxruntime_pybind11_state). The official hooks
# (pyinstaller-hooks-contrib) handle them; hiddenimports above is enough.
for pkg in (
    "docling",
    "docling_core",
    "docling_parse",
    "docling_ibm_models",
    "litellm",
    "tiktoken",
    "tiktoken_ext",
    "magika",
    "rapidocr",
    "google.genai",
    "webview",            # pywebview platform backends load dynamically
    "sudachipy",
    "sudachidict_core",
    "dashscope",
    "lightrag",
    "openai",
    "yt_dlp",
):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue  # optional package not installed on the build machine
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(SPEC_DIR, "launch_gui.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest",
        "IPython",
        "matplotlib",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DocIngest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(BUILD, "docingest.ico") if os.path.isfile(os.path.join(BUILD, "docingest.ico")) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DocIngest",
)
