"""
Unicode-script classification for language-consistency checks.

Maps each code point to a *script* name (Latin, Han, Hiragana, Katakana,
Hangul, Bengali, Thai, ...). Used by vision triage to detect pages whose
extracted text contains scripts that are incompatible with the document's
declared language — a strong signal that Docling's CMap decoding failed.

Ranges come from the Unicode standard. Only the scripts we care about are
listed explicitly; everything else maps to "Other" so the caller can treat
them uniformly. Adding a language support later = extending the caller's
whitelist config, not editing this file.

Why bisect a range table instead of regex or unicodedata?
  * regex alternation over dozens of ranges is slow for large pages.
  * stdlib unicodedata.name() returns strings like "HIRAGANA LETTER A" that
    must still be parsed; doing the bucket ourselves is faster and lets us
    define "Common" (digits / spaces / punctuation) exactly how triage needs it.
  * No new dependencies.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter


# Unicode block ranges, in ascending start order. Each entry is
#   (start_codepoint_inclusive, end_codepoint_inclusive, script_name).
#
# "Common" is a catch-all for text that every language uses — digits,
# whitespace, punctuation, CJK punctuation, fullwidth ASCII. Classifying
# these as "Common" (rather than their nominal script) means a Japanese
# page containing "2026年4月" or an English page with Unicode quotes does
# not trigger the language-inconsistency check.
#
# When a code point falls outside every listed range it is "Other" —
# callers usually treat this as neither expected nor unexpected, folding
# it into whichever count makes sense for their heuristic.
_RAW_RANGES: tuple[tuple[int, int, str], ...] = (
    # Common: whitespace, ASCII digits and punctuation, general punctuation,
    # CJK symbols/punctuation, fullwidth forms, currency, math, arrows, geometric.
    (0x0000, 0x001F, "Common"),                 # C0 controls (tab, LF, CR)
    (0x0020, 0x002F, "Common"),
    (0x0030, 0x0039, "Common"),                 # ASCII digits
    (0x003A, 0x0040, "Common"),
    (0x005B, 0x0060, "Common"),
    (0x007B, 0x007E, "Common"),
    (0x00A0, 0x00BF, "Common"),                 # Latin-1 punctuation / symbols
    (0x2000, 0x206F, "Common"),                 # General punctuation
    (0x2070, 0x209F, "Common"),                 # Super/subscripts
    (0x20A0, 0x20CF, "Common"),                 # Currency
    (0x2100, 0x214F, "Common"),                 # Letterlike (℃ № etc.)
    (0x2150, 0x218F, "Common"),                 # Number forms
    (0x2190, 0x21FF, "Common"),                 # Arrows
    (0x2200, 0x22FF, "Common"),                 # Mathematical operators
    (0x2500, 0x257F, "Common"),                 # Box drawing
    (0x25A0, 0x25FF, "Common"),                 # Geometric shapes
    (0x2600, 0x26FF, "Common"),                 # Misc symbols
    (0x2700, 0x27BF, "Common"),                 # Dingbats
    (0x3000, 0x303F, "Common"),                 # CJK symbols & punctuation
    (0xFF00, 0xFF5E, "Common"),                 # Fullwidth ASCII
    (0xFF5F, 0xFFEF, "Common"),                 # Fullwidth punctuation / half-width forms
    (0xFFF0, 0xFFFF, "Common"),                 # Specials (incl. U+FFFD — caller handles separately)

    # Latin
    (0x0041, 0x005A, "Latin"),                  # ASCII uppercase
    (0x0061, 0x007A, "Latin"),                  # ASCII lowercase
    (0x00C0, 0x00FF, "Latin"),                  # Latin-1 letters (accented)
    (0x0100, 0x017F, "Latin"),                  # Latin Extended-A
    (0x0180, 0x024F, "Latin"),                  # Latin Extended-B
    (0x1E00, 0x1EFF, "Latin"),                  # Latin Extended Additional

    # Greek / Cyrillic
    (0x0370, 0x03FF, "Greek"),
    (0x0400, 0x04FF, "Cyrillic"),
    (0x0500, 0x052F, "Cyrillic"),

    # Semitic
    (0x0590, 0x05FF, "Hebrew"),
    (0x0600, 0x06FF, "Arabic"),
    (0x0750, 0x077F, "Arabic"),

    # Indic — the common CMap-failure destination for corrupt CJK PDFs
    (0x0900, 0x097F, "Devanagari"),
    (0x0980, 0x09FF, "Bengali"),
    (0x0A00, 0x0A7F, "Gurmukhi"),
    (0x0A80, 0x0AFF, "Gujarati"),
    (0x0B00, 0x0B7F, "Oriya"),
    (0x0B80, 0x0BFF, "Tamil"),
    (0x0C00, 0x0C7F, "Telugu"),
    (0x0C80, 0x0CFF, "Kannada"),
    (0x0D00, 0x0D7F, "Malayalam"),
    (0x0D80, 0x0DFF, "Sinhala"),

    # Southeast Asian
    (0x0E00, 0x0E7F, "Thai"),
    (0x0E80, 0x0EFF, "Lao"),
    (0x0F00, 0x0FFF, "Tibetan"),
    (0x1000, 0x109F, "Myanmar"),
    (0x1780, 0x17FF, "Khmer"),

    # CJK (Chinese characters)
    (0x2E80, 0x2EFF, "Han"),                    # CJK radicals supplement
    (0x2F00, 0x2FDF, "Han"),                    # Kangxi radicals
    (0x3400, 0x4DBF, "Han"),                    # CJK Ext-A
    (0x4E00, 0x9FFF, "Han"),                    # CJK unified ideographs
    (0xF900, 0xFAFF, "Han"),                    # CJK compatibility
    (0x20000, 0x2A6DF, "Han"),                  # CJK Ext-B
    (0x2A700, 0x2B73F, "Han"),                  # CJK Ext-C
    (0x2B740, 0x2B81F, "Han"),                  # CJK Ext-D

    # Japanese kana
    (0x3040, 0x309F, "Hiragana"),
    (0x30A0, 0x30FF, "Katakana"),
    (0x31F0, 0x31FF, "Katakana"),               # Katakana phonetic extensions

    # Korean
    (0x1100, 0x11FF, "Hangul"),                 # Jamo
    (0x3130, 0x318F, "Hangul"),                 # Compatibility jamo
    (0xAC00, 0xD7AF, "Hangul"),                 # Syllables
)

# Runtime-sorted by start code point so bisect_right gives the correct
# "largest range whose start ≤ cp" answer. Authoring order above is
# grouped by purpose (readable); this guarantees correctness without
# requiring authors to manually interleave groups.
_RANGES: tuple[tuple[int, int, str], ...] = tuple(sorted(_RAW_RANGES))
_STARTS: tuple[int, ...] = tuple(r[0] for r in _RANGES)


def script_for(codepoint: int) -> str:
    """Return the script name for a single Unicode code point."""
    idx = bisect_right(_STARTS, codepoint) - 1
    if idx < 0:
        return "Other"
    start, end, name = _RANGES[idx]
    if start <= codepoint <= end:
        return name
    return "Other"


def classify(text: str) -> Counter:
    """
    Count characters in `text` by script name.

    Returns a Counter so callers can inspect both the dominant script and
    the exact distribution (useful for logging).
    """
    counts: Counter = Counter()
    for ch in text:
        counts[script_for(ord(ch))] += 1
    return counts


def unexpected_script_ratio(
    text: str,
    expected_scripts: set[str] | frozenset[str],
) -> tuple[float, Counter]:
    """
    Compute the ratio of characters whose script is NOT in `expected_scripts`.

    "Common" (digits, spaces, punctuation, CJK punctuation, fullwidth forms)
    is always treated as expected regardless of what the caller passes —
    every language uses those. "Other" is treated as unexpected (safer to
    surface for Vision review than to silently accept).

    Returns (ratio, per-script counts). Ratio is 0.0 for empty / all-Common
    text — callers should pair this with a min-length guard to avoid
    false positives on very short strings.
    """
    counts = classify(text)
    total = sum(counts.values())
    if total == 0:
        return 0.0, counts

    expected = set(expected_scripts)
    expected.add("Common")  # always allowed

    unexpected = sum(
        count for script, count in counts.items()
        if script not in expected
    )
    return unexpected / total, counts
