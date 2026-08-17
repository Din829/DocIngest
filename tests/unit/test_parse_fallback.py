"""
Parse-failure fallback: a Docling parse that dies must not lose the whole file.

Before this behaviour existed, a parse timeout returned immediately from
``process_single_file`` — and the Vision step that could have read the file
lives in a LATER Phase it never reached. A 17-page Japanese PDF with a perfectly
good text layer was simply lost.

What these tests pin down:

1. A PDF whose parse times out is recovered through the vision_only engine.
2. The recovery is VISIBLE — run warnings + chunk lineage. A silent downgrade
   would be worse than the failure, because the file would look normal while
   its content came from a different engine with different properties.
3. ``on_parse_failure: skip`` restores the old drop-the-file behaviour.
4. Formats vision_only does NOT read itself never trigger a retry (they delegate
   back to Docling, so retrying would re-run the parse that just failed).
5. The timeout message names the knob that actually produced the budget.
6. Under vision_only, language is detected on Vision's text, not on the
   pagebreak markers that are all the parser emits.

Fully offline: the Vision call is patched at ``describe_image``, and the parse
timeout is forced to ~0 s rather than waiting on a genuinely slow file.

Run:
    python tests/unit/test_parse_fallback.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docingest import ingest                          # noqa: E402
from docingest.config import load_config              # noqa: E402
import docingest.models.provider as provider_mod      # noqa: E402
import docingest.pipeline as pipeline                 # noqa: E402

JP_TEXT = "出入国管理及び難民認定法別表第一の二の表の高度専門職の項の下欄の基準を定める省令"


def _make_vertical_pdf(tmp: Path, name: str = "vertical.pdf") -> Path:
    """A PDF shaped like vertically typeset Japanese: one glyph per text object,
    stacked into columns. This is the shape that makes layout analysis emit a
    one-glyph-per-cell pseudo-table."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    x = 500.0
    for col in range(6):
        y = 80.0
        for ch in JP_TEXT[:20]:
            page.insert_text((x, y), ch, fontsize=12, fontname="china-ss")
            y += 14
        x -= 20
    out = tmp / name
    doc.save(str(out))
    doc.close()
    return out


def _make_pdf(tmp: Path, name: str = "sample.pdf") -> Path:
    """A real 2-page PDF — the fallback renders pages, so it needs a real file."""
    import pymupdf

    doc = pymupdf.open()
    for n in (1, 2):
        page = doc.new_page()
        page.insert_text((72, 100), f"page {n}", fontsize=14)
    out = tmp / name
    doc.save(str(out))
    doc.close()
    return out


class _Patched:
    """Make the Docling parse time out, and stub Vision. Restores both on exit.

    The timeout is injected by raising from the Docling parser itself rather
    than by shrinking ``_resolve_parse_timeout``: the budget must stay realistic
    so the FALLBACK gets a real one too. Shrinking the shared budget starves the
    retry as well and tests a scenario that cannot happen — in production the
    fallback inherits the same (ample) budget the failed parse had.

    Manual patching (not pytest fixtures) so this file also runs as a plain
    script, which is how the rest of the suite is invoked.
    """

    def __init__(self, vision_text: str = "recovered by vision", fail_parse: bool = True):
        self.vision_text = vision_text
        self.fail_parse = fail_parse

    def __enter__(self):
        import docingest.parsers as parsers_mod
        import docingest.parsers.vision as vision_mod

        self._parsers_mod = parsers_mod
        self._vision_mod = vision_mod
        self._real_docling_parse = parsers_mod._DoclingWithFallback.parse
        self._real_vision = provider_mod.describe_image
        self._real_vision_bound = vision_mod.describe_image

        if self.fail_parse:
            def _timeout(self, *a, **k):
                raise TimeoutError("timed out after 171.0s")
            parsers_mod._DoclingWithFallback.parse = _timeout

        provider_mod.describe_image = lambda *a, **k: self.vision_text
        # vision.py imported the symbol directly — patch that binding too.
        vision_mod.describe_image = lambda *a, **k: self.vision_text
        return self

    def __exit__(self, *exc):
        self._parsers_mod._DoclingWithFallback.parse = self._real_docling_parse
        provider_mod.describe_image = self._real_vision
        self._vision_mod.describe_image = self._real_vision_bound
        return False


def _run(inp: Path, out: Path, **overrides):
    base = {"knowledge_map.enabled": False, "run_log.enabled": False}
    base.update(overrides)
    return ingest([inp], output=out, config_overrides=base)


def test_fallback_recovers_a_timed_out_pdf():
    print("=== test_fallback_recovers_a_timed_out_pdf ===")
    tmp = Path(tempfile.mkdtemp(prefix="dc_fb_in_"))
    out = Path(tempfile.mkdtemp(prefix="dc_fb_out_"))
    try:
        pdf = _make_pdf(tmp)
        with _Patched(vision_text=JP_TEXT):
            result = _run(pdf, out)

        assert result.stats["failed"] == 0, result.stats["errors"]
        assert result.stats["successful"] == 1, result.stats
        body = "\n".join(md["content"] for md in result.markdown_files)
        assert JP_TEXT in body, "content should come from the Vision fallback"
        print("  PASSED\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_fallback_is_visible_in_warnings_and_lineage():
    """A downgrade nobody can see is the real failure mode."""
    print("=== test_fallback_is_visible_in_warnings_and_lineage ===")
    tmp = Path(tempfile.mkdtemp(prefix="dc_fb_in_"))
    out = Path(tempfile.mkdtemp(prefix="dc_fb_out_"))
    try:
        pdf = _make_pdf(tmp)
        with _Patched():
            result = _run(pdf, out)

        warned = " ".join(str(w) for w in result.stats.get("warnings", []))
        assert "vision_only" in warned, f"no fallback warning surfaced: {warned!r}"
        assert "bounding box" in warned, "warning must state what the downgrade costs"

        steps = [
            t for c in result.chunks
            for t in c["metadata"].get("lineage", {}).get("transformations", [])
        ]
        kinds = {t.get("step") for t in steps}
        assert "parse_fallback" in kinds, f"lineage missing parse_fallback: {kinds}"
        # Provenance must name the engine that actually produced the text,
        # not the parser object that was passed in and failed.
        parser_names = {t.get("name") for t in steps if t.get("step") == "parser"}
        assert parser_names == {"VisionOnlyParser"}, parser_names
        print("  PASSED\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_skip_restores_old_behaviour():
    print("=== test_skip_restores_old_behaviour ===")
    tmp = Path(tempfile.mkdtemp(prefix="dc_fb_in_"))
    out = Path(tempfile.mkdtemp(prefix="dc_fb_out_"))
    try:
        pdf = _make_pdf(tmp)
        with _Patched():
            result = _run(pdf, out, **{"error_handling.on_parse_failure": "skip"})

        assert result.stats["failed"] == 1, result.stats
        assert result.stats["successful"] == 0, result.stats
        err = result.stats["errors"][0]
        assert err["error_type"] == "timeout", err
        print("  PASSED\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_no_fallback_for_formats_vision_only_delegates():
    """A .txt retry would re-run the same path — never worth Vision spend."""
    print("=== test_no_fallback_for_formats_vision_only_delegates ===")
    tmp = Path(tempfile.mkdtemp(prefix="dc_fb_in_"))
    out = Path(tempfile.mkdtemp(prefix="dc_fb_out_"))
    try:
        txt = tmp / "note.txt"
        txt.write_text("hello world\n", encoding="utf-8")

        calls: list[str] = []
        real = pipeline._try_vision_only_fallback

        def spy(file_path, config, parse_timeout, reason):
            got = real(file_path, config, parse_timeout, reason)
            calls.append(f"{file_path.suffix}->{got is not None}")
            return got

        pipeline._try_vision_only_fallback = spy
        try:
            with _Patched():
                _run(txt, out)
        finally:
            pipeline._try_vision_only_fallback = real

        assert all(c.endswith("False") for c in calls), calls
        print("  PASSED\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_failed_fallback_reports_the_original_error():
    """When the retry ALSO fails, the user must see why the parse failed —
    not a second, more confusing error from the recovery attempt."""
    print("=== test_failed_fallback_reports_the_original_error ===")
    tmp = Path(tempfile.mkdtemp(prefix="dc_fb_in_"))
    out = Path(tempfile.mkdtemp(prefix="dc_fb_out_"))
    try:
        pdf = _make_pdf(tmp)
        import docingest.parsers.vision_only_parser as vo_mod
        real_vo_parse = vo_mod.VisionOnlyParser.parse

        def _boom(self, *a, **k):
            raise RuntimeError("render backend exploded")

        vo_mod.VisionOnlyParser.parse = _boom
        try:
            with _Patched():
                result = _run(pdf, out)
        finally:
            vo_mod.VisionOnlyParser.parse = real_vo_parse

        assert result.stats["failed"] == 1, result.stats
        err = result.stats["errors"][0]
        assert err["error_type"] == "timeout", err
        assert "render backend exploded" not in err["error"], (
            "the fallback's own error must not replace the original cause"
        )
        print("  PASSED\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_timeout_message_names_the_real_knob():
    """max_sec is a ceiling; pointing users at it when they never hit it is a
    guaranteed no-op — the exact trap this message used to set."""
    print("=== test_timeout_message_names_the_real_knob ===")
    tmp = Path(tempfile.mkdtemp(prefix="dc_fb_in_"))
    out = Path(tempfile.mkdtemp(prefix="dc_fb_out_"))
    try:
        pdf = _make_pdf(tmp)
        with _Patched():
            result = _run(pdf, out, **{"error_handling.on_parse_failure": "skip"})

        msg = result.stats["errors"][0]["error"]
        assert "base_sec" in msg and "per_page_sec" in msg, msg
        assert "raising max_sec changes" in msg or "reached max_sec" in msg, msg
        assert "vision_only" in msg, "should offer the engine that skips parsing"
        print("  PASSED\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_language_detected_on_vision_text_not_pagebreaks():
    """vision_only emits only pagebreak markers before Vision runs; detecting
    language there classified a Japanese document as English."""
    print("=== test_language_detected_on_vision_text_not_pagebreaks ===")
    tmp = Path(tempfile.mkdtemp(prefix="dc_fb_in_"))
    out = Path(tempfile.mkdtemp(prefix="dc_fb_out_"))
    try:
        pdf = _make_pdf(tmp)
        with _Patched(vision_text=JP_TEXT, fail_parse=False):
            result = _run(pdf, out, **{"parsing.engine": "vision_only"})

        langs = {md["metadata"].get("language") for md in result.markdown_files}
        assert langs == {"ja"}, f"expected ja from Vision text, got {langs}"
        print("  PASSED\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_vertical_pdf_routed_before_the_doomed_parse():
    """The fallback would recover a vertical PDF anyway — but only after the
    doomed parse runs to completion (a timeout marks a call failed, it cannot
    interrupt it). Detecting up front is what actually saves the time."""
    print("=== test_vertical_pdf_routed_before_the_doomed_parse ===")
    tmp = Path(tempfile.mkdtemp(prefix="dc_vt_in_"))
    out = Path(tempfile.mkdtemp(prefix="dc_vt_out_"))
    try:
        pdf = _make_vertical_pdf(tmp)
        from docingest.pipeline import _looks_vertical_pdf
        cfg = load_config()
        assert _looks_vertical_pdf(pdf, cfg), "probe should flag this shape"

        # fail_parse=True proves Docling is never even reached: if it were, the
        # patched parser would raise and we would see a fallback, not a route.
        with _Patched(vision_text=JP_TEXT, fail_parse=True):
            result = _run(pdf, out)

        assert result.stats["failed"] == 0, result.stats["errors"]
        steps = [
            t for c in result.chunks
            for t in c["metadata"].get("lineage", {}).get("transformations", [])
        ]
        kinds = {t.get("step") for t in steps}
        assert "parse_route" in kinds, f"expected an up-front route: {kinds}"
        assert "parse_fallback" not in kinds, "must not have gone through a failed parse"
        warned = " ".join(str(w) for w in result.stats.get("warnings", []))
        assert "Vertical CJK" in warned, warned
        print("  PASSED\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_horizontal_pdf_is_not_routed():
    print("=== test_horizontal_pdf_is_not_routed ===")
    tmp = Path(tempfile.mkdtemp(prefix="dc_vt_in_"))
    try:
        from docingest.pipeline import _looks_vertical_pdf
        cfg = load_config()
        assert not _looks_vertical_pdf(_make_pdf(tmp), cfg)
        # Real-world corpus: the probe must stay silent on ordinary documents.
        for name in ("nutrition.pdf", "BehaviouralInsights.pdf"):
            f = ROOT / "test_docs" / name
            if f.exists():
                assert not _looks_vertical_pdf(f, cfg), f"false positive on {name}"
        print("  PASSED\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_probe_is_safe_on_broken_and_textless_files():
    """The probe must never decide anything on a guess."""
    print("=== test_probe_is_safe_on_broken_and_textless_files ===")
    tmp = Path(tempfile.mkdtemp(prefix="dc_vt_in_"))
    try:
        from docingest.pipeline import _looks_vertical_pdf
        cfg = load_config()

        not_a_pdf = tmp / "broken.pdf"
        not_a_pdf.write_bytes(b"this is not a pdf at all")
        assert not _looks_vertical_pdf(not_a_pdf, cfg)

        import pymupdf
        doc = pymupdf.open()
        doc.new_page()                      # a page with no text layer
        blank = tmp / "blank.pdf"
        doc.save(str(blank))
        doc.close()
        assert not _looks_vertical_pdf(blank, cfg)

        # And it must be switchable off.
        vert = _make_vertical_pdf(tmp)
        off = load_config(cli_overrides={"parsing": {"vertical_detect": {"enabled": False}}})
        assert not _looks_vertical_pdf(vert, off)
        print("  PASSED\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    load_config()  # fail fast if the YAML itself is broken
    tests = [
        test_fallback_recovers_a_timed_out_pdf,
        test_fallback_is_visible_in_warnings_and_lineage,
        test_skip_restores_old_behaviour,
        test_no_fallback_for_formats_vision_only_delegates,
        test_failed_fallback_reports_the_original_error,
        test_timeout_message_names_the_real_knob,
        test_language_detected_on_vision_text_not_pagebreaks,
        test_vertical_pdf_routed_before_the_doomed_parse,
        test_horizontal_pdf_is_not_routed,
        test_probe_is_safe_on_broken_and_textless_files,
    ]
    for t in tests:
        t()
    print(f"All {len(tests)} parse-fallback tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
