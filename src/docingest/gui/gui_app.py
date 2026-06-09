"""
Shell layer — launches the pywebview window and loads the hand-written
frontend. This is the only file that knows pywebview exists; swapping shells
(PyQt, ...) means rewriting just this file. No business logic here.

Run:  python -m docingest.gui
"""

from __future__ import annotations

import json
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
            if paths:
                window.evaluate_js(
                    f"window.__onFilesDropped({json.dumps(paths, ensure_ascii=False)})"
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
        load_dotenv()
    except ImportError:
        pass
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
    webview.start()


if __name__ == "__main__":
    main()
