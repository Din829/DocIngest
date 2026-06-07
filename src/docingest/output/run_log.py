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

    # Special path 1: safety abort — the run was refused before any file was
    # processed. Surface this loudly so a "log says nothing" reading is wrong:
    # the run DID happen, it was DENIED.
    if pipeline_result.safety.get("aborted"):
        return _build_aborted_section(pipeline_result, timestamp)

    # Group by status. Unknown/empty status falls under "cached" (safest:
    # legacy callers that didn't tag a status probably reused outputs).
    by_status: dict[str, list["FileResult"]] = {
        "added": [], "updated": [], "cached": [], "forced": [], "failed": [],
    }
    for fr in pipeline_result.files:
        bucket = fr.status if fr.status in by_status else "cached"
        by_status[bucket].append(fr)

    total = pipeline_result.total_files
    processed = sum(len(v) for v in by_status.values())  # files actually visited
    summary = _summary_phrase(by_status, force_rebuild)

    # Special path 2: interrupted — Ctrl+C between files. The header SHOUTS
    # because the resulting knowledge base is partial; downstream RAG / agents
    # need to know "this isn't the full ingestion" without reading the body.
    if pipeline_result.interrupted:
        header_prefix = "INTERRUPTED"
        # When interrupted, total_files = planned, processed = how far we got.
        # The 'after N/M' phrasing makes the partial state obvious in greps.
        count_phrase = f"after {processed}/{total} files"
    else:
        header_prefix = "run" + (" (forced)" if force_rebuild else "")
        count_phrase = f"{total} files"

    # Header tail: elapsed + LLM usage are otherwise only visible in the
    # terminal output of THIS run — once you close the terminal, gone.
    # Persisting them in log.md makes "what did that run cost?" greppable
    # weeks later.
    tail_bits = [_format_elapsed(pipeline_result.elapsed_ms)]
    llm_phrase = _format_llm_summary(pipeline_result.token_usage)
    if llm_phrase:
        tail_bits.append(llm_phrase)
    vision_phrase = _format_vision_triage(
        getattr(pipeline_result, "vision_triage", {}) or {}
    )
    if vision_phrase:
        tail_bits.append(vision_phrase)
    tail = " | ".join(tail_bits)

    lines = [
        f"## [{timestamp}] {header_prefix} | {count_phrase} ({summary}) | {tail}",
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


def _build_aborted_section(
    pipeline_result: "PipelineResult", timestamp: str,
) -> str:
    """
    Render the section for a safety-aborted run.

    These runs processed ZERO files (Phase 0 refused), so the regular
    `added / cached / failed` framing doesn't apply. We surface the
    violation summary so the user can see, weeks later, exactly which
    files / metrics tripped the budget gate without re-running inspect.
    """
    safety = pipeline_result.safety
    summary = safety.get("summary") or {}
    violations = safety.get("violations") or []
    mode = safety.get("mode", "strict")

    # Header summary numbers come from the Phase 0 summary, NOT from
    # pipeline_result.total_files (which can be 0 here).
    total_files = summary.get("total_files", len(violations))
    total_pages = summary.get("total_pages")
    cost = summary.get("total_est_cost_usd")
    tail_bits: list[str] = []
    if total_pages is not None:
        tail_bits.append(f"{total_pages} pages")
    if cost is not None:
        tail_bits.append(f"~${cost:.2f}")
    tail = " | " + " | ".join(tail_bits) if tail_bits else ""

    lines = [
        f"## [{timestamp}] ABORTED BY SAFETY ({mode}) | "
        f"{total_files} files refused | {len(violations)} violations{tail}",
        "",
    ]

    # Body: one bullet per violating file with the specific reasons.
    # Keep each reason terse (Phase 0 already formats them concisely).
    for v in violations[:50]:  # cap for log hygiene on huge batches
        name = Path(v.get("file", "?")).name
        reasons = v.get("reasons") or []
        reason_text = "; ".join(str(r) for r in reasons) if reasons else "exceeded budget"
        lines.append(f"- refused: {name} — {reason_text}")
    if len(violations) > 50:
        lines.append(f"- ... and {len(violations) - 50} more violations")
    lines.append("")

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
        # Tag the error class so downstream readers (humans, agents, log
        # parsers) can branch without grepping the message — "[timeout]"
        # means "retry might work", "[parse_error]" means "tune config or
        # accept loss". Empty error_type stays untagged (legacy callers).
        tag = f" [{fr.error_type}]" if fr.error_type else ""
        return f"- failed{tag}: {name} — {err_short}"

    chunks_str = (
        f"{fr.chunks_count} chunks"
        if fr.chunks_count
        else "no chunks"
    )

    if status == "updated" and fr.cache_reason:
        return f"- updated: {name} ({fr.cache_reason}) → {chunks_str}"

    return f"- {status}: {name} → {chunks_str}"


# ---------------------------------------------------------------------------
# Header tail helpers — elapsed time + LLM token usage
#
# These two numbers are otherwise only visible in the terminal of the run
# that produced them. Persisting compact forms in log.md lets "how long did
# that run take / how much LLM did it burn?" stay greppable across history.
# ---------------------------------------------------------------------------

def _format_elapsed(elapsed_ms: int) -> str:
    """
    Render elapsed time compactly: sub-minute → '47.5s', minutes → '2m13s'.
    Returns '0s' for None / 0 (rare, but a no-op run can land here).
    """
    if not elapsed_ms:
        return "0s"
    total_seconds = elapsed_ms / 1000.0
    if total_seconds < 60:
        return f"{total_seconds:.1f}s"
    minutes = int(total_seconds // 60)
    seconds = int(total_seconds % 60)
    return f"{minutes}m{seconds:02d}s"


def _format_llm_summary(token_usage: dict) -> str:
    """
    Render the LLM-call summary tail, e.g. 'LLM: 1 call / 2607 tok'.

    Returns '' when no LLM was called (the most common case — Vision triage
    plus knowledge_map disabled). When some calls happened, surface BOTH the
    call count and total tokens because cost is roughly proportional to both
    and one number alone can mislead (1 tiny call vs 1 huge call look the
    same on just 'calls').
    """
    if not token_usage:
        return ""
    calls = token_usage.get("total_calls", 0)
    if not calls:
        return ""
    total = token_usage.get("total_tokens", 0)
    call_word = "call" if calls == 1 else "calls"
    return f"LLM: {calls} {call_word} / {total:,} tok"


def _format_vision_triage(vt: dict) -> str:
    """
    Render the Vision triage tail, e.g.
    'Vision: 12/50 pages sent, 38 skipped (7 furniture)'.

    Shows at a glance how Vision cost broke down: of the pages that carried
    pictures, how many actually went to Vision vs. were skipped by triage, and
    how many of those skips were furniture (logo/header — the savings from
    parsing.vision.triage.furniture_exempt). Returns '' when no paged file ran
    Vision triage (nothing to report). The furniture clause is omitted when 0.
    """
    if not vt:
        return ""
    sent = vt.get("sent_to_vision", 0)
    skipped = vt.get("triage_skipped", 0)
    pages = vt.get("pages_with_pictures", 0)
    if not (sent or skipped or pages):
        return ""
    furn = vt.get("furniture_skipped", 0)
    furn_clause = f" ({furn} furniture)" if furn else ""
    return f"Vision: {sent}/{pages} pages sent, {skipped} skipped{furn_clause}"
