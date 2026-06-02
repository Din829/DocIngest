"""
js_api bridge — the object pywebview exposes to the JS frontend.

JS calls these as ``window.pywebview.api.<method>(...)`` and gets a Promise.
The bridge only marshals dict/list/native types (decoupling rule); all real
work goes through gui_logic. Long tasks (ingest) run on a background thread
and push progress back to JS via ``window.evaluate_js`` so the UI thread
never blocks — see BACKEND_API §A.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from . import gui_logic


def _js_arg(value: Any) -> str:
    """JSON-encode a value for safe interpolation into an evaluate_js call."""
    return json.dumps(value, ensure_ascii=False)


class Api:
    """Methods here are the frontend API surface. Keep them thin: validate /
    delegate to gui_logic / return plain data. The pywebview Window is
    injected after creation so background tasks can push events to JS."""

    def __init__(self) -> None:
        self._window: Any = None

    def bind_window(self, window: Any) -> None:
        """Called by gui_app after the window exists. Lets background threads
        reach window.evaluate_js for progress push."""
        self._window = window

    # -- synchronous, fast calls (return straight to the JS Promise) --------

    def inspect(self, paths: list[str]) -> dict[str, Any]:
        """Pre-flight: {files, totals, run_violations} for the cost dialog."""
        return gui_logic.inspect_paths(list(paths))

    def pick_files(self) -> list[str]:
        """Open the native multi-select file dialog and return chosen paths
        (empty list on cancel). pywebview 6.x uses webview.FileDialog.OPEN;
        the dialog returns a tuple of paths or None."""
        import webview

        if self._window is None:
            return []
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True
        )
        return list(result) if result else []

    def list_libraries(self) -> list[dict[str, Any]]:
        return gui_logic.list_libraries()

    def get_summary(self, library_dir: str) -> dict[str, Any]:
        return gui_logic.library_summary(library_dir)

    def preview_markdown(self, library_dir: str, filename: str) -> str:
        return gui_logic.preview_markdown(library_dir, filename)

    def start_refine(
        self,
        library_dir: str,
        files: list[str],
        skill: str,
        acknowledge: bool = False,
    ) -> dict[str, Any]:
        """Refine on a background thread (it calls an LLM per file — slow).
        refine_files has no per-file progress callback, so the frontend shows
        a single "整形中…" state, then a terminal result. Returns immediately.

        Large files split + refine in parallel. When the cost gate trips
        (refine.cost_check.mode=strict, over budget), the run is blocked and the
        UI gets __onRefineBlocked with the estimate; it confirms with the user
        and re-calls start_refine(acknowledge=True).

        Event channels:
          window.__onRefineDone(result)    — {files:[...]}
          window.__onRefineBlocked(info)   — {estimate, reasons} (cost gate)
          window.__onRefineError(message)  — unexpected failure
        """
        lib = str(library_dir)
        file_list = list(files)
        sk = str(skill)
        ack = bool(acknowledge)

        def _push(fn: str, payload: Any) -> None:
            if self._window is not None:
                self._window.evaluate_js(f"window.{fn}({_js_arg(payload)})")

        def _worker() -> None:
            try:
                result = gui_logic.refine(lib, file_list, sk, acknowledge=ack)
                if result.get("blocked"):
                    _push("__onRefineBlocked", {
                        "estimate": result.get("estimate"),
                        "reasons": result.get("reasons", []),
                    })
                else:
                    _push("__onRefineDone", result)
            except Exception as e:
                _push("__onRefineError", str(e))

        threading.Thread(target=_worker, daemon=True).start()
        return {"started": True}

    def graph_status(self, library_dir: str) -> dict[str, Any]:
        """Is a graph built for this library? {available, built, counts}."""
        return gui_logic.graph_status(library_dir)

    def start_build_graph(self, library_dir: str) -> dict[str, Any]:
        """Build the knowledge graph on a background thread (LLM per chunk —
        slow + costs money). Per-chunk progress is pushed; completion/failure
        are terminal events. Returns immediately.

        Event channels:
          window.__onGraphProgress(event)  — {current,total,chunk_id,status}
          window.__onGraphDone(result)     — {entities,relations,communities,...}
          window.__onGraphError(message)   — unexpected failure
        """
        lib = str(library_dir)

        def _push(fn: str, payload: Any) -> None:
            if self._window is not None:
                self._window.evaluate_js(f"window.{fn}({_js_arg(payload)})")

        def _worker() -> None:
            try:
                result = gui_logic.build_graph(
                    lib, on_progress=lambda ev: _push("__onGraphProgress", ev)
                )
                _push("__onGraphDone", result)
            except Exception as e:
                _push("__onGraphError", str(e))

        threading.Thread(target=_worker, daemon=True).start()
        return {"started": True}

    def doctor(self) -> dict[str, Any]:
        return gui_logic.doctor()

    def get_settings(self) -> dict[str, Any]:
        return gui_logic.load_settings()

    def effective_safety(self) -> dict[str, Any]:
        """Resolved safety thresholds for the cost-limit screen's initial values."""
        return gui_logic.effective_safety()

    def save_settings(self, settings: dict[str, Any]) -> str:
        return gui_logic.store_settings(dict(settings))

    def open_folder(self, path: str) -> bool:
        """Open a library folder in the OS file manager. Pure shell concern,
        lives at the bridge edge (not gui_logic — it's UI, not data)."""
        import os
        import subprocess
        import sys

        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
            return True
        except Exception:
            return False

    # -- long-running: ingest on a background thread, progress → JS ---------

    def start_ingest(
        self,
        paths: list[str],
        library_name: str,
        options: dict[str, Any] | None = None,
        acknowledge_large: bool = False,
    ) -> dict[str, Any]:
        """Kick off an ingest on a background thread and return immediately
        with ``{"started": True}``. Progress is pushed to JS as it happens;
        completion / failure are pushed as terminal events. The frontend
        renders screen 03 from these events and transitions to 04 on done.

        Event channels (JS-side handlers):
          window.__onIngestProgress(event)  — per-file event dict
          window.__onIngestDone(summary)    — final summary dict
          window.__onIngestError(message)   — unexpected failure
        """
        path_list = list(paths)
        opts = dict(options) if options else None

        def _push(fn: str, payload: Any) -> None:
            if self._window is not None:
                self._window.evaluate_js(f"window.{fn}({_js_arg(payload)})")

        def _worker() -> None:
            try:
                summary = gui_logic.run_ingest(
                    path_list,
                    library_name,
                    options=opts,
                    acknowledge_large=acknowledge_large,
                    on_progress=lambda ev: _push("__onIngestProgress", ev),
                )
                _push("__onIngestDone", summary)
            except Exception as e:  # surface unexpected failures to the UI
                _push("__onIngestError", str(e))

        threading.Thread(target=_worker, daemon=True).start()
        return {"started": True}
