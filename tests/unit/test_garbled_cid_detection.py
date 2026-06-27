"""
Garbled-output detection — broken CID font (no glyph<> marker).

Regression for a real PWC PDF where a page's embedded font lacked a
ToUnicode map. Docling's markdown path emitted CID-soup (``0g0M0K0`` runs plus
stray Latin-1 symbols ÿ ¥ ¶ …) with NO ``glyph<>`` marker, so the old
_detect_garbled (which only counted ``glyph<``) returned False, the pymupdf
fallback never fired, and ~214 broken runs / a whole page of Japanese reached
the output as garbage — while pymupdf reads the same page perfectly.

The fix adds a second, AND-gated density signal. These tests pin:
  1. CID-soup IS detected (fallback would fire)
  2. glyph<> path still works (backward compat)
  3. normal prose is NOT flagged — including copyright/units symbol lines,
     which lift the weird-symbol ratio but never the X0X run ratio.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.pipeline import _detect_garbled


def _pad(s: str, target: int = 2500) -> str:
    """Repeat to clear the >=2000 length gate without changing char ratios."""
    return (s + "\n") * (target // len(s) + 1)


# --- Signal 2: broken CID soup (the bug) ---------------------------------

def test_cid_soup_detected():
    # Shape mirrors the real Docling output: X0X runs + stray Latin-1 symbols.
    soup = "## ÿ %¡ eåg,VýQ0kW0OO0W0f0D0f0 |sVý|M0g0o0j0D0 N N¥OOSË0g0M0K0"
    assert _detect_garbled(_pad(soup)), "broken-CID soup must be flagged"


def test_real_garbled_ratios_trip():
    # Synthetic block at the measured ratios of the real PDF (weird~0.9%, x0~1.4%).
    block = ("0g0M0K0 0W0f0D0 ÿ¥¶ N N¥OO 0k0j0 normal text mixed in here too "
             "0g0o0j 0K0W0 ã·¡ Q0kW0OO0W0f")
    assert _detect_garbled(_pad(block))


# --- Signal 1: glyph<> still works (backward compat) ---------------------

def test_glyph_marker_still_detected():
    md = "Some text " + "glyph<c=12> " * 12  # >= threshold 10
    assert _detect_garbled(md)


def test_glyph_below_threshold_not_flagged():
    # 3 glyph markers (< threshold 10) in otherwise-clean long prose. Do NOT
    # _pad here — padding would multiply the markers past the threshold.
    md = ("This is normal document text repeated to clear the length gate. " * 50
          + "glyph<c=12> " * 3)
    assert not _detect_garbled(md)


# --- Normal prose must NOT be flagged ------------------------------------

def test_normal_japanese_not_flagged():
    jp = "本人確認書類について 運転免許証 マイナンバーカード パスポート 在留カード"
    assert not _detect_garbled(_pad(jp))


def test_normal_english_not_flagged():
    en = "PwC Japan require new joiners to open two bank accounts before joining."
    assert not _detect_garbled(_pad(en))


def test_copyright_and_units_not_flagged():
    # Lifts the weird-symbol ratio (©°§µ) but has NO X0X runs → must stay clean.
    # This is the false-positive the AND gate exists to prevent.
    line = "© 2018 PwC. All rights reserved. Temperature 25° §3 µm ± 0.5 ®"
    assert not _detect_garbled(_pad(line))


def test_short_text_not_judged():
    # Below the 2000-char gate, ratios are too noisy → never flagged on signal 2.
    assert not _detect_garbled("ÿ¥¶ 0g0M0K0")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all passed")
