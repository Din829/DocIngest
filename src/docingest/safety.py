"""
Safety — pre-run budget check (Phase 0).

Runs once over the discovered file list BEFORE any parser / LLM call.
Flags files and per-run totals that exceed configured thresholds and returns
a structured violation report. Callers (run_pipeline) decide what to do
with it based on safety.mode:

  off    — Phase 0 is skipped entirely (legacy behaviour).
  warn   — log violations, keep running.
  strict — (default) refuse to run unless the caller passes
           acknowledge_large=True (Python / MCP) or --yes (CLI).

Design principles
-----------------
* **Data over file size.** Disk MB is a poor proxy for processing cost.
  A 100 MB PPTX with 20 slides is cheaper than a 5 MB PDF with 200 dense
  pages. Real drivers are:
    pages          → Vision API call count
    chars_est      → output markdown / token bulk
    rows           → xlsx processing time
    duration_sec   → ASR cost
    est_cost_usd   → rough dollar figure from provider-agnostic price table
    size_mb        → last-resort guard for formats we cannot introspect

* **Config-driven thresholds.** Every check reads a single config path;
  setting a threshold to null disables that specific dimension. Adding a
  new dimension is a one-line edit in _PER_FILE_CHECKS.

* **Never raises.** Any exception during inspection is swallowed — safety
  must not turn a completable run into a crash. The worst case is an
  under-reported cost, not a broken pipeline.

* **JSON-shaped output.** violations is plain list[dict] so both CLI
  (pretty-print) and MCP (forward to Agent) consume the same data.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .config import get_nested

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cost estimation — rough per-file Vision dollar figure
# ---------------------------------------------------------------------------
# The price table lives in config (safety.vision_price_per_call) so users
# can override per deployment. This module only provides the lookup and
# the formula. Prices are not a live feed — they change infrequently and
# operators should update config after provider pricing changes.

_DEFAULT_FALLBACK_PRICE = 0.005  # USD per Vision call; ultimate fallback


def estimate_file_cost_usd(info: dict[str, Any], config: dict[str, Any]) -> float:
    """
    Estimate Vision / video API cost for a single file.

    Two cost models, by file shape:
      * page-based docs (pdf/pptx/docx/...): cost ≈ min(pages, vision.max_pages)
        × per-call price.
      * video (no pages, has duration_sec): cost ≈ duration × per-second token
        rate × per-token price — see _estimate_video_cost_usd. Without this a
        video estimated 0.0 (pages=None), which silently defeated the cost
        pre-flight for the highest-cost input type.

    Returns 0.0 when Vision is disabled or the file has neither pages nor a
    video duration (audio-only / text / unknown formats). Safe on missing
    fields.
    """
    if not get_nested(config, "parsing.vision.enabled", True):
        return 0.0

    pages = info.get("pages") or 0
    if not pages:
        # No pages — but a video carries a real (often large) cost via its
        # duration. Estimate that instead of falling through to 0.0.
        if info.get("duration_sec") and _is_video(info):
            return _estimate_video_cost_usd(info, config)
        return 0.0

    cap_raw = get_nested(config, "parsing.vision.max_pages", 50)
    cap = int(cap_raw) if cap_raw is not None else int(pages)
    vision_calls = min(int(pages), cap)

    price = _lookup_vision_price(config)
    return vision_calls * price


# Video formats that go through Vision (native video understanding or frame
# sampling). Audio-only formats are excluded — they cost ASR, not Vision, and
# ASR cost is small + harder to price per-second here. Mirrors the video set in
# config parsing.audio.video_formats; kept as a literal so safety has no import
# dependency on the parser.
_VIDEO_EXTS = {"mp4", "avi", "mkv", "webm", "mov", "wmv", "flv", "ts", "m4v"}


def _is_video(info: dict[str, Any]) -> bool:
    return str(info.get("format", "")).lower() in _VIDEO_EXTS


def _estimate_video_cost_usd(info: dict[str, Any], config: dict[str, Any]) -> float:
    """
    Estimate the Vision cost of a video from its duration.

    Native video understanding (default) sends the whole clip to a video model
    that bills per token. Gemini tokenizes at ~258 tokens per sampled frame +
    ~32 tokens/s of audio; at the configured fps the per-second token rate is
    ``258 × fps + 32``. cost = duration_sec × tok_per_sec × price_per_token.

    Frame-sampling fallback (native_video off) bills per Vision *call* instead:
    one call per sampled frame, frame count = duration / interval, capped by
    max_frames (or the global vision.max_pages). cost = frames × per-call price.

    Both rates/prices are config-driven; 258 is Gemini's documented per-frame
    token count (a fixed protocol fact, not a tunable) so it stays inline.
    """
    duration = float(info.get("duration_sec") or 0)
    if duration <= 0:
        return 0.0

    native_on = get_nested(config, "parsing.audio.native_video.enabled", True)

    if native_on:
        fps = float(get_nested(config, "parsing.audio.native_video.fps", 1) or 1)
        tok_per_sec = 258.0 * fps + 32.0          # frames + audio, per second
        price_per_million = float(
            get_nested(config, "safety.video_token_price_per_million", 0.50)
        )
        return duration * tok_per_sec * price_per_million / 1_000_000.0

    # Frame-sampling path: one Vision call per sampled frame.
    interval = float(get_nested(config, "parsing.audio.video_frames.interval_sec", 10) or 10)
    frames = duration / interval if interval > 0 else 0
    cap_raw = get_nested(config, "parsing.audio.video_frames.max_frames", None)
    if cap_raw is None:
        cap_raw = get_nested(config, "parsing.vision.max_pages", 50)
    if cap_raw is not None:
        frames = min(frames, float(cap_raw))
    return frames * _lookup_vision_price(config)


def _lookup_vision_price(config: dict[str, Any]) -> float:
    """
    Resolve per-call price from config.safety.vision_price_per_call.

    Lookup order:
      1. Exact model name match.
      2. Prefix match either way (handles "gemini-3-flash-preview" vs
         "gemini-3-flash" style drift across config files).
      3. Explicit "_default" key.
      4. Hard fallback constant.
    """
    model = get_nested(config, "models.vision.primary.model", "") or ""
    prices = get_nested(config, "safety.vision_price_per_call", {}) or {}
    if not isinstance(prices, dict):
        return _DEFAULT_FALLBACK_PRICE

    if model and model in prices:
        return float(prices[model])

    # Prefix match (case-insensitive). Longest common prefix wins informally:
    # we iterate and take the first match, but the table is expected to be
    # small enough that ordering doesn't matter in practice.
    if model:
        model_lower = model.lower()
        for key, val in prices.items():
            if key == "_default":
                continue
            key_lower = str(key).lower()
            if model_lower.startswith(key_lower) or key_lower.startswith(model_lower):
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue

    default = prices.get("_default")
    if default is not None:
        try:
            return float(default)
        except (TypeError, ValueError):
            pass
    return _DEFAULT_FALLBACK_PRICE


# ---------------------------------------------------------------------------
# Violation checks
# ---------------------------------------------------------------------------
# Data-table driven so adding a new dimension is a single-line edit.
#   (info_field, config_path, metric_label)
# - info_field  : key that inspect_single puts into the info dict
# - config_path : safety.per_file.<…> threshold to compare against
# - metric_label: short name surfaced to CLI / MCP / Agent

_PER_FILE_CHECKS: list[tuple[str, str, str]] = [
    ("pages",        "safety.per_file.max_pages",        "pages"),
    ("chars_est",    "safety.per_file.max_chars_est",    "chars_est"),
    ("total_rows",   "safety.per_file.max_rows",         "rows"),
    ("duration_sec", "safety.per_file.max_duration_sec", "duration_sec"),
    ("size_mb",      "safety.per_file.max_size_mb",      "size_mb"),
    ("est_cost_usd", "safety.per_file.max_est_cost_usd", "est_cost_usd"),
]


def check_file_violations(info: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return reasons this file exceeds configured per-file thresholds."""
    reasons: list[dict[str, Any]] = []
    for field, cfg_path, metric in _PER_FILE_CHECKS:
        threshold = get_nested(config, cfg_path, None)
        if threshold is None:
            # null = dimension disabled
            continue
        value = info.get(field)
        if value is None:
            continue
        try:
            if value > threshold:
                reasons.append({
                    "metric": metric,
                    "value": value,
                    "threshold": threshold,
                })
        except TypeError:
            # Mismatched types (e.g. str in numeric slot) — skip silently,
            # safety must never crash the run.
            continue
    return reasons


def check_run_violations(infos: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return reasons the whole run exceeds per_run thresholds."""
    reasons: list[dict[str, Any]] = []
    total_pages = sum((i.get("pages") or 0) for i in infos)
    total_cost = sum((i.get("est_cost_usd") or 0.0) for i in infos)
    total_files = len(infos)

    checks = [
        ("max_total_pages",        total_pages, "total_pages"),
        ("max_total_files",        total_files, "total_files"),
        ("max_total_est_cost_usd", total_cost,  "total_est_cost_usd"),
    ]
    for cfg_key, value, metric in checks:
        threshold = get_nested(config, f"safety.per_run.{cfg_key}", None)
        if threshold is None:
            continue
        try:
            if value > threshold:
                reasons.append({
                    "metric": metric,
                    "value": round(value, 4) if isinstance(value, float) else value,
                    "threshold": threshold,
                })
        except TypeError:
            continue
    return reasons


# ---------------------------------------------------------------------------
# Entry point — used by pipeline.run_pipeline and by the inspect command
# ---------------------------------------------------------------------------

def check_budget(
    files: list[Path],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Run Phase 0 budget inspection on a pre-discovered file list.

    Args:
        files: Output of discover_files — every item is an existing local path.
        config: Full merged config dict.

    Returns:
        (violations, summary)

        violations : list of {file, reasons} dicts, plus optionally one
                     {scope: "per_run", reasons} entry. Empty list means
                     every file and the run total are within budget.

        summary    : {total_files, total_pages, total_est_cost_usd, infos}
                     infos is the full per-file inspection output (same shape
                     as inspect_single) so callers can surface extra detail.
    """
    # Lazy import to avoid cycle (inspect imports safety too).
    from .inspect import inspect_single

    violations: list[dict[str, Any]] = []
    infos: list[dict[str, Any]] = []

    for f in files:
        try:
            info = inspect_single(f, config)
        except Exception as e:
            # inspect must not crash budget checks; degrade to size-only info.
            logger.debug(f"Safety inspect failed for {f.name}: {e}")
            try:
                size_mb = f.stat().st_size / (1024 * 1024)
            except OSError:
                size_mb = 0.0
            info = {
                "name": f.name,
                "path": str(f),
                "format": f.suffix.lstrip(".").lower(),
                "size_mb": round(size_mb, 2),
                "inspect_error": str(e),
            }
        infos.append(info)
        file_reasons = check_file_violations(info, config)
        if file_reasons:
            violations.append({"file": f.name, "reasons": file_reasons})

    run_reasons = check_run_violations(infos, config)
    if run_reasons:
        violations.append({"scope": "per_run", "reasons": run_reasons})

    summary = {
        "total_files": len(files),
        "total_pages": sum((i.get("pages") or 0) for i in infos),
        "total_est_cost_usd": round(
            sum((i.get("est_cost_usd") or 0.0) for i in infos), 4
        ),
        "infos": infos,
    }
    return violations, summary


# ---------------------------------------------------------------------------
# Rendering for CLI / logs
# ---------------------------------------------------------------------------

def format_violations(violations: list[dict[str, Any]]) -> str:
    """
    Human-readable rendering of the violation list for log / CLI output.

    Example::
        report.pdf: pages=320 > 50; est_cost_usd=3.2 > 1.0
        [per_run]: total_pages=640 > 200

    Numeric values get thousands separators; non-numeric values pass through
    as-is (safe when threshold config has been customised with strings).
    """
    if not violations:
        return ""
    lines: list[str] = []
    for v in violations:
        label = v.get("file") or f"[{v.get('scope', 'unknown')}]"
        parts: list[str] = []
        for r in v.get("reasons", []):
            val = r.get("value")
            thr = r.get("threshold")
            if isinstance(val, (int, float)) and isinstance(thr, (int, float)):
                parts.append(f"{r.get('metric')}={val:,} > {thr:,}")
            else:
                parts.append(f"{r.get('metric')}={val} > {thr}")
        lines.append(f"  {label}: {'; '.join(parts)}")
    return "\n".join(lines)
