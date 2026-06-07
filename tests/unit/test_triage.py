"""
Vision triage tests — focused on the Latin-script cipher-garble layer
(_has_latin_cipher_garble), the 10th skip-check, and its integration into
_should_skip_vision.

Background: a broken font CMap can map each glyph to a DIFFERENT legal Latin
letter ("Nutrition" → "Pgvsdvdfp"). The output is clean ASCII (no glyph<, no
U+FFFD, no CJK, all "Latin" script), so triage checks 5-9 all pass and the
unreadable page would be skipped. This layer flags it via an abnormally low
vowel ratio and sends the page to Vision instead.

No LLM calls. The real-corpus false-positive / recall assertions read genuine
output under knowledge/ when present and skip gracefully when it is absent
(fresh checkout / CI) so the suite always runs.

Run:
    python tests/unit/test_triage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docingest.config import load_config, get_nested
from docingest.pipeline import _has_latin_cipher_garble, _should_skip_vision
from docingest.parsers.base import PageData


def _triage_cfg() -> dict:
    return get_nested(load_config(), "parsing.vision.triage", {})


# A clean English paragraph (no frontmatter / markup) used as the canonical
# negative sample.
NORMAL_EN = (
    "Nutrition is the biochemical and physiological process by which an "
    "organism uses food to support its life. It provides organisms with "
    "nutrients, which can be metabolized to create energy and chemical "
    "structures. Failure to obtain the required amount of nutrients causes "
    "malnutrition. Nutritional science studies how the body breaks down food."
)


# ---------------------------------------------------------------------------
# Cipher helpers — reproduce a broken-CMap substitution at test time so the
# assertions don't depend on any committed fixture.
# ---------------------------------------------------------------------------

def _substitution_map() -> dict:
    """Bijective a-z permutation that maps every vowel onto a consonant
    (mirrors how a permutation collapses the vowel ratio)."""
    src = "abcdefghijklmnopqrstuvwxyz"
    vowels = "aeiou"
    cons = [c for c in src if c not in vowels]
    m: dict = {}
    for v, c in zip(vowels, cons[:5]):
        m[v] = c
    for c, v in zip(cons[:5], vowels):
        m[c] = v
    rest = cons[5:]
    for i, c in enumerate(rest):
        m[c] = rest[(i + 1) % len(rest)]
    return m


def _encipher(text: str, m: dict) -> str:
    out = []
    for ch in text:
        lo = ch.lower()
        if lo in m:
            out.append(m[lo].upper() if ch.isupper() else m[lo])
        else:
            out.append(ch)
    return "".join(out)


def _caesar(text: str, shift: int = 3) -> str:
    out = []
    for ch in text:
        if "a" <= ch <= "z":
            out.append(chr((ord(ch) - 97 + shift) % 26 + 97))
        elif "A" <= ch <= "Z":
            out.append(chr((ord(ch) - 65 + shift) % 26 + 65))
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Pure-function behaviour
# ---------------------------------------------------------------------------

def test_normal_english_is_not_cipher():
    print("=== test_normal_english_is_not_cipher ===")
    assert _has_latin_cipher_garble(NORMAL_EN, _triage_cfg()) is False
    print("  PASSED\n")


def test_substitution_cipher_is_flagged():
    print("=== test_substitution_cipher_is_flagged ===")
    cipher = _encipher(NORMAL_EN, _substitution_map())
    assert _has_latin_cipher_garble(cipher, _triage_cfg()) is True
    print("  PASSED\n")


def test_caesar_cipher_is_flagged():
    print("=== test_caesar_cipher_is_flagged ===")
    assert _has_latin_cipher_garble(_caesar(NORMAL_EN), _triage_cfg()) is True
    print("  PASSED\n")


def test_too_few_letters_is_not_flagged():
    """Short / number-heavy strings can't be judged statistically → never flagged."""
    print("=== test_too_few_letters_is_not_flagged ===")
    assert _has_latin_cipher_garble("Q3 2025: 12,400 / 8.5%", _triage_cfg()) is False
    assert _has_latin_cipher_garble("brx", _triage_cfg()) is False  # < min_letters
    print("  PASSED\n")


def test_cjk_dominant_page_is_excluded():
    """A Japanese page with a few inline English words must not be cipher-flagged,
    even if the Latin slice alone looks vowel-poor."""
    print("=== test_cjk_dominant_page_is_excluded ===")
    # Mostly Japanese, with a low-vowel English fragment embedded.
    ja_page = "これは日本語の文書です。" * 10 + " TLS rtv cnf wrk spec"
    assert _has_latin_cipher_garble(ja_page, _triage_cfg()) is False
    print("  PASSED\n")


def test_disabled_switch_turns_it_off():
    print("=== test_disabled_switch_turns_it_off ===")
    cfg = dict(_triage_cfg())
    cfg["latin_cipher_check"] = {"enabled": False}
    cipher = _encipher(NORMAL_EN, _substitution_map())
    assert _has_latin_cipher_garble(cipher, cfg) is False
    print("  PASSED\n")


def test_missing_subconfig_defaults_on():
    """A config predating this layer (no latin_cipher_check key) still protects."""
    print("=== test_missing_subconfig_defaults_on ===")
    cfg = dict(_triage_cfg())
    cfg.pop("latin_cipher_check", None)
    cipher = _encipher(NORMAL_EN, _substitution_map())
    assert _has_latin_cipher_garble(cipher, cfg) is True
    print("  PASSED\n")


# ---------------------------------------------------------------------------
# Integration — the blind spot the layer closes, through the real entry point
# ---------------------------------------------------------------------------

def test_should_skip_vision_blindspot_closed():
    """End-to-end: a clean text page skips Vision, a ciphered one does NOT —
    proving the layer reaches _should_skip_vision and flips the decision."""
    print("=== test_should_skip_vision_blindspot_closed ===")
    cfg = _triage_cfg()
    cipher = _encipher(NORMAL_EN, _substitution_map())

    normal_page = PageData(page_no=1, text=NORMAL_EN, num_pictures=0)
    cipher_page = PageData(page_no=1, text=cipher, num_pictures=0)

    # Normal English: pure text → safe to skip.
    assert _should_skip_vision(normal_page, {}, cfg, doc_language="en") is True
    # Cipher (en) and (unknown language): must be sent to Vision.
    assert _should_skip_vision(cipher_page, {}, cfg, doc_language="en") is False
    assert _should_skip_vision(cipher_page, {}, cfg, doc_language=None) is False
    print("  PASSED\n")


# ---------------------------------------------------------------------------
# Real-corpus guard rails — 0 false positives, full recall. Skips when the
# knowledge/ fixtures aren't present (fresh checkout / CI).
# ---------------------------------------------------------------------------

def _split_pages(md: str) -> list[str]:
    import re
    parts = (md.split("<!-- pagebreak -->") if "<!-- pagebreak -->" in md
             else re.split(r"\n\s*\n", md))
    pages = []
    for p in parts:
        body = "\n".join(
            line for line in p.splitlines()
            if not line.strip().startswith(
                ("source:", "format:", "title:", "language:",
                 "pages:", "mimetype:", "---"))
        ).strip()
        if len(body) >= 50:
            pages.append(body)
    return pages


def _load_real_pages(rel_paths: list[str]) -> list[str]:
    out: list[str] = []
    for rel in rel_paths:
        f = ROOT / rel
        if f.exists():
            out.extend(_split_pages(f.read_text(encoding="utf-8", errors="ignore")))
    return out


def test_real_corpus_zero_false_positive_full_recall():
    print("=== test_real_corpus_zero_false_positive_full_recall ===")
    cfg = _triage_cfg()
    en_pages = _load_real_pages([
        "knowledge/IEA_WEO_100p/sources/WorldEnergyOutlook2025.md",
        "knowledge/nda/sources/NDA-HongboDing.md",
    ])
    ja_pages = _load_real_pages([
        "knowledge/nutrition/sources/nutrition.md",
        "knowledge/入社書類案内_full/sources/入社書類案内.md",
        "knowledge/pwc_docusign/sources/【採用候補者の方へ】 PwCリファレンスガイド .md",
    ])

    if not en_pages:
        print("  SKIPPED (no knowledge/ fixtures present)\n")
        return

    # 0 false positives on real EN + JA pages.
    en_fp = [p for p in en_pages if _has_latin_cipher_garble(p, cfg)]
    ja_fp = [p for p in ja_pages if _has_latin_cipher_garble(p, cfg)]
    assert not en_fp, f"{len(en_fp)} real EN pages wrongly flagged"
    assert not ja_fp, f"{len(ja_fp)} real JA pages wrongly flagged"

    # Full recall on the same EN pages, ciphered two different ways.
    sub = _substitution_map()
    sub_missed = [p for p in en_pages if not _has_latin_cipher_garble(_encipher(p, sub), cfg)]
    cae_missed = [p for p in en_pages if not _has_latin_cipher_garble(_caesar(p), cfg)]
    assert not sub_missed, f"{len(sub_missed)}/{len(en_pages)} substitution-cipher pages missed"
    assert not cae_missed, f"{len(cae_missed)}/{len(en_pages)} caesar-cipher pages missed"

    print(f"  EN pages={len(en_pages)} JA pages={len(ja_pages)} "
          f"| FP=0 | recall=100% (2 cipher families)")
    print("  PASSED\n")


def main():
    # Pure function
    test_normal_english_is_not_cipher()
    test_substitution_cipher_is_flagged()
    test_caesar_cipher_is_flagged()
    test_too_few_letters_is_not_flagged()
    test_cjk_dominant_page_is_excluded()
    test_disabled_switch_turns_it_off()
    test_missing_subconfig_defaults_on()
    # Integration
    test_should_skip_vision_blindspot_closed()
    # Real corpus
    test_real_corpus_zero_false_positive_full_recall()
    print("ALL triage tests PASSED")


if __name__ == "__main__":
    main()
