# -*- coding: utf-8 -*-
"""Unit tests for _is_authoritative_text — the full-mode text-layer trust
gate. Healthy embedded-text-layer reads upgrade the prompt's page_text
section to AUTHORITATIVE wording; any check failing keeps the default
wording (pre-feature behaviour)."""

from docingest.parsers.vision import _is_authoritative_text

_ON: dict = {}  # default config → enabled (default true)
_OFF = {"parsing": {"vision": {"text_authority": {"enabled": False}}}}


def test_healthy_japanese_text_is_authoritative():
    text = "人事本部　本部長　瀧澤　明良\n下記の者は弊社を退職であることを証明致します。"
    assert _is_authoritative_text(text, _ON) is True


def test_empty_text_is_not_authoritative():
    # Scans: text layer is empty → Vision must rely on its eyes.
    assert _is_authoritative_text("", _ON) is False
    assert _is_authoritative_text("   \n  ", _ON) is False


def test_glyph_garble_is_not_authoritative():
    # CMap-damaged PDFs emit glyph<NNN> markers (Phase 1.1 case).
    assert _is_authoritative_text("契約glyph<123>条件", _ON) is False
    assert _is_authoritative_text("契約glyph&lt;123&gt;条件", _ON) is False


def test_replacement_char_ratio_gate():
    # > 5% U+FFFD → damaged extraction, not trustworthy.
    bad = "あ" + "�" * 10
    assert _is_authoritative_text(bad, _ON) is False
    # A stray replacement char in long healthy text stays authoritative.
    ok = "正常なテキスト" * 20 + "�"
    assert _is_authoritative_text(ok, _ON) is True


def test_rollback_switch():
    text = "完全に健全なテキストです。"
    assert _is_authoritative_text(text, _OFF) is False


def test_short_text_is_still_authoritative():
    # Deliberately NO min-length gate: a cover page with four characters of
    # embedded text is still character-exact truth (over-defence removed).
    assert _is_authoritative_text("退職証明書", _ON) is True
