"""
Systemic Vision-failure tests — the fail-loud/fail-safe boundary.

Covers the new _enrich_with_vision behaviour: when EVERY Vision page call
fails AND the failed pages were content-critical (scanned-empty or garbled),
surface it per parsing.vision.on_systemic_failure (error/warn/ignore), while
NEVER touching the single-page fallback.

Boundary under test (must all hold true together to fail loud):
  A (scale):       described == 0 and failed > 0
  B (criticality): >= min_critical_pages failed pages were content-critical
Plus: described > 0 (any page enriched) must NEVER trigger — the partial-success
case keeps the old silent fallback.

No real LLM calls — describe_page_cached is monkeypatched to raise, which drives
the exact same per-page except path a real key-failure takes.

Run:
    python tests/unit/test_vision_systemic_failure.py
"""

from __future__ import annotations

import sys
import copy
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docingest.config import load_config, get_nested
from docingest import pipeline
from docingest.pipeline import (
    _is_vision_critical_page,
    _enrich_with_vision,
    VisionSystemicFailure,
)
from docingest.parsers.base import ParseResult, PageData


def _cfg(on_systemic="error", min_critical=1, triage_enabled=False):
    """Config with cache off (so failures really fire) and the systemic knob set."""
    c = copy.deepcopy(load_config())
    c.setdefault("cache", {})["enabled"] = False
    v = c.setdefault("parsing", {}).setdefault("vision", {})
    v["enabled"] = True
    v["max_pages"] = None
    v["on_systemic_failure"] = on_systemic
    v.setdefault("systemic_failure", {})["min_critical_pages"] = min_critical
    v.setdefault("triage", {})["enabled"] = triage_enabled
    return c


def _tri_sysf():
    c = load_config()
    return (
        get_nested(c, "parsing.vision.triage", {}),
        get_nested(c, "parsing.vision.systemic_failure", {}),
    )


def _pages(texts):
    """Build PageData list; image_path just needs to be truthy (Vision is mocked)."""
    return [
        PageData(page_no=i + 1, text=t, image_path=f"fake-page-{i+1}.png")
        for i, t in enumerate(texts)
    ]


def _parse_result(texts, markdown="body"):
    return ParseResult(
        markdown=markdown,
        metadata={"format": "pdf", "title": "t", "language": "en"},
        pages=_pages(texts),
    )


class _DescribeStub:
    """Context-managerless monkeypatch of describe_page_cached.

    mode='fail'  → every page raises (simulates total key failure).
    mode='one_fail' → first page raises, the rest return text (partial success).
    """

    def __init__(self, mode):
        self.mode = mode
        self._orig = None
        self._n = 0

    def __enter__(self):
        self._orig = pipeline.__dict__.get("describe_page_cached")
        # _enrich_with_vision imports describe_page_cached locally; patch the
        # source module symbol so the local import picks up the stub.
        import docingest.parsers.vision as vmod
        self._vorig = vmod.describe_page_cached

        def stub(*args, **kwargs):
            self._n += 1
            if self.mode == "fail":
                raise RuntimeError("Vision description failed: SIMULATED key failure")
            if self.mode == "one_fail":
                if self._n == 1:
                    raise RuntimeError("Vision failed: SIMULATED single-page failure")
                return "real vision text for this page"
            raise AssertionError("unknown mode")

        vmod.describe_page_cached = stub
        return self

    def __exit__(self, *a):
        import docingest.parsers.vision as vmod
        vmod.describe_page_cached = self._vorig


# ---------------------------------------------------------------------------
# _is_vision_critical_page — pure-function unit checks
# ---------------------------------------------------------------------------

def test_critical_page_empty_and_placeholders():
    tri, sysf = _tri_sysf()
    assert _is_vision_critical_page("", tri, sysf) is True
    assert _is_vision_critical_page("   \n ", tri, sysf) is True
    assert _is_vision_critical_page("<!-- image -->", tri, sysf) is True
    assert _is_vision_critical_page("<!-- image -->\n<!-- pagebreak -->", tri, sysf) is True
    print("  critical: empty / placeholder pages flagged  PASSED")


def test_critical_page_garble():
    tri, sysf = _tri_sysf()
    assert _is_vision_critical_page("glyph<c=5> glyph<c=9> aaa bbb ccc ddd", tri, sysf) is True
    print("  critical: glyph< garble flagged  PASSED")


def test_clean_text_is_not_critical():
    tri, sysf = _tri_sysf()
    clean = ("This is a clean paragraph of genuine document text with plenty "
             "of ordinary readable words that Docling captured perfectly well.")
    assert _is_vision_critical_page(clean, tri, sysf) is False
    print("  critical: clean text NOT flagged  PASSED")


# ---------------------------------------------------------------------------
# _enrich_with_vision — systemic decision
# ---------------------------------------------------------------------------

def test_scanned_total_failure_raises_by_default():
    """described==0, all pages scanned-empty → error (default) raises."""
    pr = _parse_result(["<!-- image -->", "<!-- image -->", "<!-- image -->"])
    with _DescribeStub("fail"):
        raised = False
        try:
            _enrich_with_vision(pr, _cfg(on_systemic="error"))
        except VisionSystemicFailure as e:
            raised = True
            assert "Systemic Vision failure" in str(e)
            assert "SIMULATED" in str(e)  # first real error surfaced
    assert raised, "expected VisionSystemicFailure on scanned total failure"
    vt = pr.metadata["vision_triage"]
    assert vt["described"] == 0 and vt["failed"] == 3 and vt["critical_failed"] == 3
    print("  scanned + total failure + error → raises, tally persisted  PASSED")


def test_clean_text_total_failure_does_not_raise():
    """described==0 but every failed page had CLEAN Docling text → no raise
    (Vision was only supplementing; the page content survives as fallback)."""
    clean = "A fully readable paragraph of real text " * 4
    pr = _parse_result([clean, clean, clean])
    with _DescribeStub("fail"):
        _enrich_with_vision(pr, _cfg(on_systemic="error"))  # must NOT raise
    vt = pr.metadata["vision_triage"]
    assert vt["described"] == 0 and vt["failed"] == 3
    assert vt["critical_failed"] == 0, "clean pages must not count as critical"
    print("  clean text + total failure → no raise (supplement-only)  PASSED")


def test_partial_success_never_triggers():
    """described > 0 must NEVER trigger systemic failure, even with scanned
    pages — this is the single-page-failure-doesn't-误杀 guarantee."""
    pr = _parse_result(["<!-- image -->", "<!-- image -->", "<!-- image -->"])
    with _DescribeStub("one_fail"):
        _enrich_with_vision(pr, _cfg(on_systemic="error"))  # must NOT raise
    vt = pr.metadata["vision_triage"]
    assert vt["described"] >= 1, "stub should have described >=1 page"
    assert vt["failed"] >= 1, "stub should have failed the first page"
    # described>0 → systemic check is skipped regardless of critical_failed.
    print(f"  partial success (described={vt['described']}, failed={vt['failed']}) "
          f"→ no raise  PASSED")


def test_ignore_is_backward_compatible():
    """on_systemic_failure=ignore → legacy silent behaviour even when scanned
    pages all fail."""
    pr = _parse_result(["<!-- image -->", "<!-- image -->"])
    with _DescribeStub("fail"):
        _enrich_with_vision(pr, _cfg(on_systemic="ignore"))  # must NOT raise
    vt = pr.metadata["vision_triage"]
    assert vt["described"] == 0 and vt["critical_failed"] == 2
    print("  ignore → no raise (backward compat escape hatch)  PASSED")


def test_warn_logs_error_does_not_raise(caplog_level=logging.ERROR):
    """on_systemic_failure=warn → no raise, but an ERROR-level log line."""
    pr = _parse_result(["<!-- image -->", "<!-- image -->"])
    records = []

    class _Catch(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Catch()
    handler.setLevel(logging.ERROR)
    vlog = logging.getLogger("docingest.pipeline")
    vlog.addHandler(handler)
    try:
        with _DescribeStub("fail"):
            _enrich_with_vision(pr, _cfg(on_systemic="warn"))  # must NOT raise
    finally:
        vlog.removeHandler(handler)
    err_lines = [r for r in records if r.levelno >= logging.ERROR
                 and "systemic vision failure" in r.getMessage().lower()]
    assert err_lines, "warn mode must emit an ERROR-level systemic-failure log"
    print("  warn → ERROR log, no raise  PASSED")


def test_min_critical_pages_threshold():
    """min_critical_pages=3 but only 2 critical pages fail → below threshold,
    no raise. Proves the threshold is honoured."""
    # 2 scanned (critical) + nothing else; threshold raised to 3.
    pr = _parse_result(["<!-- image -->", "<!-- image -->"])
    with _DescribeStub("fail"):
        _enrich_with_vision(pr, _cfg(on_systemic="error", min_critical=3))  # no raise
    vt = pr.metadata["vision_triage"]
    assert vt["critical_failed"] == 2
    print("  min_critical_pages threshold honoured (2 < 3 → no raise)  PASSED")


def main():
    print("--- _is_vision_critical_page (pure) ---")
    test_critical_page_empty_and_placeholders()
    test_critical_page_garble()
    test_clean_text_is_not_critical()
    print("--- _enrich_with_vision (systemic decision) ---")
    test_scanned_total_failure_raises_by_default()
    test_clean_text_total_failure_does_not_raise()
    test_partial_success_never_triggers()
    test_ignore_is_backward_compatible()
    test_warn_logs_error_does_not_raise()
    test_min_critical_pages_threshold()
    print("ALL systemic-failure tests PASSED")


if __name__ == "__main__":
    main()
