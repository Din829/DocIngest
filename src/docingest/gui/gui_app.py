"""
Shell layer — launches the pywebview window and loads the hand-written
frontend. This is the only file that knows pywebview exists; swapping shells
(PyQt, ...) means rewriting just this file. No business logic here.

Run:  python -m docingest.gui
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .gui_api import Api


def _index_html_path() -> Path:
    """Locate web/index.html, relative to this module. Works in-tree; the
    packaged-exe data layout is verified at packaging time (the web/ dir must
    be shipped next to this module via --add-data)."""
    return Path(__file__).resolve().parent / "web" / "index.html"


def _install_drop_handler(window: Any) -> None:
    """Wire native drag-and-drop. The browser can't expose dropped files' real
    paths to JS (security), so pywebview surfaces them on the PYTHON side as
    `pywewbviewFullPath`. We register the drop handler here, extract the paths,
    and push them to JS (window.__onFilesDropped) where they join the selection
    like picked files. The handler is bound after the DOM is loaded (the dom
    tree isn't available before that)."""
    from webview.dom import DOMEventHandler

    def _on_drop(event: dict) -> None:
        try:
            files = (event.get("dataTransfer") or {}).get("files") or []
            paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
            if not paths:
                return
            # Format gate: only processable files join the selection;
            # directories pass through (expanded recursively later). Push
            # accepted FIRST — addInputs clears the rejection note, so the
            # reverse order would wipe the note we are about to show.
            from . import gui_logic
            split = gui_logic.split_supported(paths)
            if split["accepted"]:
                window.evaluate_js(
                    f"window.__onFilesDropped({json.dumps(split['accepted'], ensure_ascii=False)})"
                )
            if split["rejected"]:
                window.evaluate_js(
                    f"window.__onFilesRejected({json.dumps(split['rejected'], ensure_ascii=False)})"
                )
        except Exception:
            pass  # drop is best-effort; never crash the UI

    def _bind() -> None:
        # True/True = let the event propagate but prevent the browser default
        # (which would otherwise navigate to the dropped file). The JS side
        # handles dragover preventDefault + hover feedback.
        window.dom.document.events.drop += DOMEventHandler(_on_drop, True, True)

    window.events.loaded += _bind


def main() -> None:
    import webview

    # Mirror the CLI's startup sequence so the GUI process has the same
    # environment view from the first action — without this, .env-supplied
    # keys are only loaded when doctor() runs (it calls load_dotenv inline),
    # so a non-doctor action (e.g. ingest, graph build) hitting a provider
    # right after launch wouldn't find them. We also hydrate user-settings
    # API keys into the environ here so a key entered earlier in the GUI
    # survives a restart (single source of truth = environ; see gui_logic
    # ._sync_api_keys_to_environ).
    try:
        from dotenv import load_dotenv
        # Packaged exe: a .env placed NEXT TO DocIngest.exe is the supported
        # way to configure API keys without the GUI settings screen. dotenv's
        # own frozen fallback uses cwd, which matches the exe dir on a plain
        # double-click but drifts when launched via a shortcut / another tool
        # — so resolve the exe-side path explicitly first. load_dotenv() then
        # still runs for the cwd/.env dev behaviour (it never overrides vars
        # the first call already set).
        if getattr(sys, "frozen", False):
            load_dotenv(Path(sys.executable).parent / ".env")
        load_dotenv()
    except ImportError:
        pass
    # Bundled binaries / models (packaged exe): point SOFFICE_PATH / FFMPEG_PATH
    # / FFPROBE_PATH / DOCLING_ARTIFACTS_PATH at what ships inside the bundle.
    # Same call the CLI and MCP entry points make — the GUI needs it too or a
    # packaged GUI silently loses LibreOffice / ffmpeg / offline models. After
    # load_dotenv (a .env-supplied path wins); no-op when running from source.
    from ..utils.bundled_binaries import ensure_bundled_binaries
    ensure_bundled_binaries()
    from . import gui_logic
    gui_logic.hydrate_environ_from_settings()

    api = Api()
    window = webview.create_window(
        "DocIngest",
        url=str(_index_html_path()),
        js_api=api,
        width=1200,
        height=920,
        min_size=(960, 720),
        background_color="#FFFFFF",
    )
    api.bind_window(window)
    _install_drop_handler(window)
    try:
        webview.start()
    except Exception as e:
        # On Windows the most likely startup failure is a missing Edge
        # WebView2 Runtime (rare — Win10/11 normally ship it, but stripped
        # enterprise images exist). A console traceback is invisible in a
        # windowed (console=False) exe, so surface a native message box with
        # the actionable fix. Other platforms / causes: re-raise as before.
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "ウィンドウエンジンを起動できませんでした。\n\n"
                "多くの場合 Microsoft Edge WebView2 Runtime が未インストール"
                "であることが原因です。以下からインストールして再起動して"
                "ください：\nhttps://developer.microsoft.com/microsoft-edge/webview2/\n\n"
                f"詳細: {e}",
                "DocIngest — 起動エラー",
                0x10,  # MB_ICONERROR
            )
        raise


if __name__ == "__main__":
    main()
