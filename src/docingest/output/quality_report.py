"""
Quality report — scan sources/*.md for Vision uncertainty markers.

The Vision prompt instructs the AI to mark unreadable content explicitly:
  - `[?]`            — partial read (e.g. "¥1,234,5[?]")
  - `[unreadable]`   — truly illegible content

This module scans the generated sources/*.md files for these markers and
produces a summary. The report helps identify documents that need manual
review or a higher image DPI / better scan.

Stateless, pure post-processing — reads from disk, writes a JSON report,
returns a summary dict. No dependency on pipeline internals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# Regex patterns for the uncertainty markers used by the Vision prompt.
# [?]         — partial read marker (e.g. "¥1,234,5[?]")
# [unreadable] — fully illegible marker. The Vision prompt allows these forms:
#   [unreadable]
#   [unreadable: top-left node]     (colon + descriptive suffix)
#   [unreadable node]               (space + role hint, from flowchart rules)
# Regex accepts anything inside the brackets that starts with "unreadable"
# (case-insensitive) so all current and reasonable future variants match.
_QUESTION_RE = re.compile(r"\[\?\]")
_UNREADABLE_RE = re.compile(r"\[unreadable\b[^\]]*\]", re.IGNORECASE)


def scan_file(md_path: Path) -> dict[str, Any]:
    """
    Count uncertainty markers in a single Markdown file.

    Args:
        md_path: Path to a sources/*.md file.

    Returns:
        Dict with counts and context lines. Empty counts if file is clean.
    """
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception as e:
        return {
            "file": str(md_path),
            "error": f"read failed: {e}",
            "question_count": 0,
            "unreadable_count": 0,
            "samples": [],
        }

    q_matches = _QUESTION_RE.findall(text)
    u_matches = _UNREADABLE_RE.findall(text)

    # Collect a few sample lines (context) for the first few markers
    samples: list[dict[str, Any]] = []
    if q_matches or u_matches:
        lines = text.split("\n")
        for line_no, line in enumerate(lines, 1):
            if _QUESTION_RE.search(line) or _UNREADABLE_RE.search(line):
                samples.append({
                    "line": line_no,
                    "text": line.strip()[:200],
                })
                if len(samples) >= 5:
                    break

    return {
        "file": str(md_path),
        "question_count": len(q_matches),
        "unreadable_count": len(u_matches),
        "samples": samples,
    }


def generate_report(
    sources_dir: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """
    Scan all Markdown files under sources_dir and build an aggregate report.

    Args:
        sources_dir: Directory containing sources/*.md files (the knowledge/sources/).
        output_path: If provided, write the full report as JSON here.

    Returns:
        Aggregate summary dict with keys:
          - total_files: total md files scanned
          - files_with_issues: count of files containing any marker
          - total_questions: sum of [?] markers across all files
          - total_unreadable: sum of [unreadable] markers
          - files: per-file details (only files with issues)
          - quality_score: 0.0-1.0, 1.0 = zero uncertainty
    """
    if not sources_dir.exists():
        return {
            "total_files": 0,
            "files_with_issues": 0,
            "total_questions": 0,
            "total_unreadable": 0,
            "files": [],
            "quality_score": 1.0,
        }

    md_files = sorted(sources_dir.rglob("*.md"))
    all_files: list[dict[str, Any]] = []
    files_with_issues: list[dict[str, Any]] = []
    total_questions = 0
    total_unreadable = 0

    for md in md_files:
        result = scan_file(md)
        all_files.append(result)
        total_questions += result["question_count"]
        total_unreadable += result["unreadable_count"]
        if result["question_count"] > 0 or result["unreadable_count"] > 0:
            files_with_issues.append(result)

    # Quality score: simple heuristic based on markers per file.
    # 1.0 = no markers. Each [unreadable] is weighted 2x a [?].
    total_weighted = total_questions + (total_unreadable * 2)
    if len(md_files) == 0:
        score = 1.0
    elif total_weighted == 0:
        score = 1.0
    else:
        # Normalize: ~10 markers per file average → 0.5 score
        avg_weighted = total_weighted / max(len(md_files), 1)
        score = max(0.0, 1.0 - (avg_weighted / 20.0))

    report = {
        "version": 1,
        "total_files": len(md_files),
        "files_with_issues": len(files_with_issues),
        "total_questions": total_questions,
        "total_unreadable": total_unreadable,
        "quality_score": round(score, 3),
        "files": files_with_issues,  # only files with issues; clean files omitted
    }

    if output_path is not None:
        try:
            output_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    return report


def format_summary(report: dict[str, Any]) -> str:
    """
    Format the report as a short human-readable summary string.

    Used by the CLI to print a one-section quality overview after a run.
    """
    total = report.get("total_files", 0)
    issues = report.get("files_with_issues", 0)
    q = report.get("total_questions", 0)
    u = report.get("total_unreadable", 0)
    score = report.get("quality_score", 1.0)

    if total == 0:
        return "No files scanned"

    if q == 0 and u == 0:
        return f"Quality: clean ({total} files, zero uncertainty markers)"

    pct = issues * 100 // max(total, 1)
    return (
        f"Quality: {issues}/{total} files ({pct}%) have uncertainty markers "
        f"— {q} [?] partial reads, {u} [unreadable] gaps "
        f"(score: {score:.2f})"
    )
