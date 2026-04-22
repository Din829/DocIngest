"""
Run log — append-only timeline of DocIngest pipeline runs.

Purpose
-------
errors.json and quality_report.json are PER-RUN snapshots — overwritten each
time the pipeline runs. log.md is different: it is the knowledge base's
TIMELINE across runs, answering "what was ingested, updated, or failed, and
when?". Inspired by Karpathy's LLM Wiki pattern (one append-only log that is
both human-readable and grep-friendly).

Design
------
* **Append-only.** Each run writes exactly one section; existing content is
  never rewritten. Survives git history naturally.
* **Grep-friendly section headers.** Every run starts with
  ``## [<ISO timestamp>] run ...`` so ``grep "^## \\[" log.md | tail -5``
  yields the last five runs regardless of body length.
* **Quiet on no-op runs.** An all-cache-hit run still writes a section
  (so log-reading tools can see "yes, the pipeline ran on <date>") but
  with an empty body — cache hits would otherwise flood the log.
* **Never raises.** Write failures downgrade to warning and the pipeline
  continues — same contract as quality_report / errors.json.
* **Not incremental-cache-relevant.** Toggling run_log.enabled does NOT
  invalidate the content cache — the log is metadata about runs, not
  about file contents. Intentionally absent from
  incremental._RELEVANT_CONFIG_PATHS.

Status vocabulary (set by pipeline.run_pipeline on each FileResult)
------------------------------------------------------------------
* ``added``   — file had no prior meta; first time ingested.
* ``updated`` — cache existed but invalidated; ``cache_reason`` explains why.
* ``cached`` — cache hit, outputs reused as-is.
* ``forced`` — ``--force`` rebuild, prior cache state irrelevant.
* ``failed`` — parse / chunk error; ``FileResult.error`` holds the message.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..pipeline import FileResult, PipelineResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def append_run_entry(
    pipeline_result: "PipelineResult",
    output_dir: Path,
    log_filename: str = "log.md",
    force_rebuild: bool = False,
) -> Path | None:
    """
    Append one Markdown section describing this run to ``<output_dir>/<log_filename>``.

    Returns the log path on success, or None on any failure (never raises —
    log problems must not break the pipeline).

    Args:
        pipeline_result: The in-memory result of the run. Only
            ``pipeline_result.files`` (each item's ``status``, ``cache_reason``,
            ``error``, ``chunks_count``, ``original_file``) and
            ``total_files`` are read.
        output_dir: Base output directory (usually ``./knowledge``).
        log_filename: File name relative to ``output_dir``. Default ``log.md``.
        force_rebuild: True when the run was invoked with ``--force`` — the
            section header and summary adapt accordingly.
    """
    log_path = output_dir / log_filename
    try:
        section = _build_section(pipeline_result, force_rebuild)
        _ensure_header(log_path)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(section)
        return log_path
    except Exception as e:
        logger.warning(f"Run log append failed ({log_path}): {e}")
        return None


# ---------------------------------------------------------------------------
# Section rendering
# ---------------------------------------------------------------------------

def _ensure_header(log_path: Path) -> None:
    """Create log.md with a top-of-file title on first write. Idempotent."""
    if log_path.exists():
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("# DocIngest Run Log\n\n", encoding="utf-8")


def _build_section(
    pipeline_result: "PipelineResult",
    force_rebuild: bool,
) -> str:
    """Render the Markdown block for one run (trailing newline included)."""
    timestamp = datetime.now().isoformat(timespec="seconds").replace("T", " ")

    # Group by status. Unknown/empty status falls under "cached" (safest:
    # legacy callers that didn't tag a status probably reused outputs).
    by_status: dict[str, list["FileResult"]] = {
        "added": [], "updated": [], "cached": [], "forced": [], "failed": [],
    }
    for fr in pipeline_result.files:
        bucket = fr.status if fr.status in by_status else "cached"
        by_status[bucket].append(fr)

    total = pipeline_result.total_files
    summary = _summary_phrase(by_status, force_rebuild)

    header_suffix = " (forced)" if force_rebuild else ""
    lines = [
        f"## [{timestamp}] run{header_suffix} | {total} files ({summary})",
        "",
    ]

    # Body: only list files that actually changed — cached stays silent so
    # a 100-file knowledge base with 1 new file produces 1 body line, not 100.
    # Order: added → updated → forced → failed (most-interesting-first).
    body_rows: list[str] = []
    for status in ("added", "updated", "forced", "failed"):
        for fr in by_status[status]:
            body_rows.append(_format_file_line(status, fr))

    if body_rows:
        lines.extend(body_rows)
        lines.append("")
    # else: empty body (no-change run) — header alone is the record.

    return "\n".join(lines) + "\n"


def _summary_phrase(
    by_status: dict[str, list["FileResult"]],
    force_rebuild: bool,
) -> str:
    """
    Build the parenthesised summary after the section header, e.g.
    '97 cached, 2 added, 1 failed' / 'no changes' / 'rebuilt'.
    """
    if force_rebuild:
        # --force means every non-failed file was rebuilt — 'rebuilt' captures
        # the intent concisely; failure count (if any) still shows in the body.
        return "rebuilt"

    added = len(by_status["added"])
    updated = len(by_status["updated"])
    cached = len(by_status["cached"])
    failed = len(by_status["failed"])

    if added == 0 and updated == 0 and failed == 0:
        return "no changes"

    bits: list[str] = []
    # Cached first so the "majority state" leads when most files are unchanged.
    if cached:
        bits.append(f"{cached} cached")
    if added:
        bits.append(f"{added} added")
    if updated:
        bits.append(f"{updated} updated")
    if failed:
        bits.append(f"{failed} failed")
    return ", ".join(bits)


def _format_file_line(status: str, fr: "FileResult") -> str:
    """Render a single per-file bullet. File name uses basename only."""
    name = Path(fr.original_file).name

    if status == "failed":
        # Trim error to one line, cap at 160 chars so a rogue stack trace
        # doesn't blow up the log width.
        err_short = (fr.error or "unknown error").split("\n", 1)[0][:160]
        return f"- failed: {name} — {err_short}"

    chunks_str = (
        f"{fr.chunks_count} chunks"
        if fr.chunks_count
        else "no chunks"
    )

    if status == "updated" and fr.cache_reason:
        return f"- updated: {name} ({fr.cache_reason}) → {chunks_str}"

    return f"- {status}: {name} → {chunks_str}"
