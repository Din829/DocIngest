"""
Knowledge-library management — the data-layer helpers a frontend (or CLI /
future web agent) needs to list, summarize, and identify processed libraries.

A "library" is one `docingest ingest` output dir (sources/ + chunks.jsonl +
index.json + ...). These helpers are pure filesystem reads/writes over those
artifacts; no UI logic. api.py thinly wraps `list_libraries` / `library_summary`
as `list_knowledge` / `get_summary`; `write_library_meta` is called by ingest.

`default_library_root()` is the GUI's choice of where libraries live; CLI/agent
don't have to use it (they pass their own output). Kept here, not in api.py's
default, so the api default (./knowledge) stays unchanged — see BACKEND_API.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

META_FILENAME = "meta.json"
INDEX_FILENAME = "index.json"
QUALITY_FILENAME = "quality_report.json"


def default_library_root() -> Path:
    """Where the GUI stores libraries: an absolute, always-writable,
    user-findable root. NOT the api default (api keeps ./knowledge) — this is
    the GUI adapter's choice, so a packaged exe doesn't drift with cwd."""
    return Path.home() / "Documents" / "DocIngest" / "knowledge"


# ---------------------------------------------------------------------------
# meta.json — written by ingest, read by the library list
# ---------------------------------------------------------------------------

def write_library_meta(
    output_dir: "Path | str",
    *,
    source_files: list[str] | None = None,
    display_name: str | None = None,
) -> Path:
    """Write `<output_dir>/meta.json` so the library list has a friendly name +
    provenance + creation time (index.json has processed_at but no user name).

    display_name defaults to the dir name. created_at is the real wall-clock
    time (this is artifact provenance, not a mock-sensitive path). Best-effort:
    a write failure here must not fail the ingest — caller ignores exceptions.
    """
    out = Path(output_dir)
    meta = {
        "display_name": display_name or out.name,
        "source_files": source_files or [],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    meta_path = out / META_FILENAME
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta_path


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Library discovery + summary
# ---------------------------------------------------------------------------

def _is_library_dir(d: Path, index_name: str = INDEX_FILENAME) -> bool:
    """An official library dir: not "_"-prefixed (those are scratch/test),
    and has an index file (a real ingest output). meta.json is the precise
    marker once present; the index keeps older meta-less libraries visible.

    index_name defaults to the standard "index.json" but is configurable
    (output.index_file) — callers reading config pass the configured name so
    a renamed index is still recognized."""
    if not d.is_dir() or d.name.startswith("_"):
        return False
    return (d / index_name).is_file()


def list_libraries(
    root: "Path | str | None" = None,
    *,
    index_name: str = INDEX_FILENAME,
) -> list[dict[str, Any]]:
    """List official libraries under `root` (default ./knowledge), newest-ish
    first by created_at when available. Each entry:
    `{name, dir, display_name, files, chunks, created_at, has_meta}`.

    index_name is the index filename (default "index.json"; configurable via
    output.index_file — the api wrapper passes the configured value).
    Tolerant: dirs without the index / unreadable ones are skipped, never
    raises — the caller (frontend list) wants a best-effort inventory.
    """
    base = Path(root) if root is not None else Path("./knowledge")
    if not base.is_dir():
        return []

    libs: list[dict[str, Any]] = []
    for d in base.iterdir():
        if not _is_library_dir(d, index_name):
            continue
        index = _read_json(d / index_name) or {}
        meta_raw = _read_json(d / META_FILENAME)
        meta = meta_raw or {}
        stats = index.get("stats", {}) or {}
        libs.append({
            "name": d.name,
            "dir": str(d.resolve()),
            "display_name": meta.get("display_name") or d.name,
            "files": stats.get("total_files"),
            "chunks": stats.get("total_chunks"),
            "created_at": meta.get("created_at") or index.get("processed_at"),
            # True = a real GUI-created library (has meta.json). The frontend
            # can choose to show only has_meta libs to hide legacy/test dirs
            # like "2" — we don't hard-filter by name (would misjudge a lib a
            # user genuinely named "2024"); meta.json is the reliable marker.
            "has_meta": meta_raw is not None,
        })

    # Sort newest first; entries without a timestamp sink to the end.
    libs.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return libs


def library_summary(
    library_dir: "Path | str",
    *,
    index_name: str = INDEX_FILENAME,
    quality_name: str = QUALITY_FILENAME,
) -> dict[str, Any]:
    """Read one library's index + quality report into a summary for the done
    screen / library detail. Returns `{dir, exists, display_name, stats,
    files, quality}`; `exists=False` when the dir isn't a library (caller
    decides how to surface it — no exception).

    index_name / quality_name default to the standard filenames but are
    configurable (output.index_file / quality.output_file) — the api wrapper
    passes the configured values."""
    d = Path(library_dir)
    if not _is_library_dir(d, index_name):
        return {"dir": str(d), "exists": False}

    index = _read_json(d / index_name) or {}
    meta = _read_json(d / META_FILENAME) or {}
    quality = _read_json(d / quality_name)  # may be absent

    return {
        "dir": str(d.resolve()),
        "exists": True,
        "display_name": meta.get("display_name") or d.name,
        "created_at": meta.get("created_at") or index.get("processed_at"),
        "stats": index.get("stats", {}),
        # Per-file inventory straight from index.json (title/pages/language/
        # chunks_count/...), the real fields the done screen shows.
        "files": index.get("files", []),
        "quality": (
            {
                "quality_score": quality.get("quality_score"),
                "files_with_issues": quality.get("files_with_issues"),
                "total_unreadable": quality.get("total_unreadable"),
            }
            if quality else None
        ),
    }
