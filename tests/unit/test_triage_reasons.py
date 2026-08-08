"""
Vision triage now names WHY a page was sent, not just how many were.

Two things are guarded here:
  1. _vision_skip_reason returns the identifier of the layer that vetoed the
     skip (so "why did page N go to Vision?" is answerable after the run).
  2. _should_skip_vision keeps its exact old boolean contract — it is now a
     wrapper, and every existing caller/test depends on that being unchanged.

The second is the important one: this refactor moved a 10-layer function body,
so equivalence with the old behaviour is the thing most likely to break.
"""

from types import SimpleNamespace

from docingest.pipeline import _should_skip_vision, _vision_skip_reason


def _page(text, page_no=1, num_pictures=0, furniture_pic_count=0):
    return SimpleNamespace(
        text=text,
        page_no=page_no,
        num_pictures=num_pictures,
        furniture_pic_count=furniture_pic_count,
        image_path="p.png",
    )


CLEAN = "This is an ordinary page of English prose. " * 5


def test_clean_page_skips_with_no_reason():
    print("=== test_clean_page_skips_with_no_reason ===")
    page = _page(CLEAN)
    assert _vision_skip_reason(page, {}, {}, "en") is None
    assert _should_skip_vision(page, {}, {}, "en") is True
    print("  clean page -> reason=None, should_skip=True")


def test_each_layer_names_itself():
    """Every veto path returns its own identifier."""
    print("=== test_each_layer_names_itself ===")
    cases = [
        ("image_marker",      _page(CLEAN + "<!-- image -->"),            {}, {}),
        ("picture_elements",  _page(CLEAN, num_pictures=2),               {}, {}),
        ("structured_data",   _page(CLEAN),                               {1: {"chart": 1}}, {}),
        ("low_text",          _page("short"),                             {}, {}),
        ("glyph_garble",      _page(CLEAN + "glyph<c=3>"),                {}, {}),
        ("replacement_chars", _page("�" * 80),                       {}, {}),
        ("complex_table",     _page("\n".join(["| a | b |"] * 12)),       {}, {}),
    ]
    for expected, page, structured, cfg in cases:
        got = _vision_skip_reason(page, structured, cfg, "en")
        print(f"  {expected:<18} -> {got!r}")
        assert got == expected, f"expected {expected}, got {got}"
        # boolean wrapper must agree
        assert _should_skip_vision(page, structured, cfg, "en") is False


def test_wrapper_equivalence_across_layers():
    """_should_skip_vision(...) == (_vision_skip_reason(...) is None), always."""
    print("=== test_wrapper_equivalence_across_layers ===")
    pages = [
        _page(CLEAN),
        _page(CLEAN + "<!-- image -->"),
        _page(CLEAN, num_pictures=1),
        _page(CLEAN, num_pictures=2, furniture_pic_count=2),
        _page("tiny"),
        _page("�" * 100),
        _page("\n".join(["| x | y |"] * 20)),
        _page(CLEAN + "glyph&lt;c=9&gt;"),
    ]
    for i, page in enumerate(pages):
        boolean = _should_skip_vision(page, {}, {}, "en")
        reason = _vision_skip_reason(page, {}, {}, "en")
        assert boolean == (reason is None), f"page {i}: {boolean} vs {reason!r}"
    print(f"  {len(pages)} pages: wrapper agrees with reason on every one")


def test_furniture_exemption_still_works():
    """
    A furniture-only page (all pictures are repeated logos, no tables) still
    skips — the picture layer must not veto it. Regression guard for the
    exemption surviving the refactor.
    """
    print("=== test_furniture_exemption_still_works ===")
    page = _page(CLEAN, num_pictures=2, furniture_pic_count=2)
    reason = _vision_skip_reason(page, {}, {}, "en")
    print(f"  furniture-only page -> {reason!r}")
    assert reason is None
    assert _should_skip_vision(page, {}, {}, "en") is True

    # One non-furniture picture → back on the Vision path.
    mixed = _page(CLEAN, num_pictures=2, furniture_pic_count=1)
    assert _vision_skip_reason(mixed, {}, {}, "en") == "picture_elements"
    print("  one non-furniture picture -> picture_elements")


def test_reason_identifiers_are_machine_readable():
    """Identifiers stay snake_case ASCII — they land in JSON keys."""
    print("=== test_reason_identifiers_are_machine_readable ===")
    import re
    pages = [
        _page(CLEAN + "<!-- image -->"),
        _page(CLEAN, num_pictures=1),
        _page("x"),
        _page("�" * 90),
    ]
    for page in pages:
        r = _vision_skip_reason(page, {}, {}, "en")
        assert r is not None and re.fullmatch(r"[a-z_]+", r), r
    print("  all identifiers match [a-z_]+")


if __name__ == "__main__":
    test_clean_page_skips_with_no_reason()
    test_each_layer_names_itself()
    test_wrapper_equivalence_across_layers()
    test_furniture_exemption_still_works()
    test_reason_identifiers_are_machine_readable()
    print("\nAll triage-reason tests passed.")
