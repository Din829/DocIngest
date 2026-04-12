"""
Keyword extractor for knowledge map — extracts meaningful keywords
from short text (document titles, section headings, sheet names).

Two extraction backends, auto-selected at runtime:

  1. SudachiPy (when installed):  morphological analysis → POS filter.
     High precision, handles Japanese compound nouns natively via Mode C.

  2. Regex fallback (zero dependencies):  improved CJK/Latin splitting
     with configurable strip patterns and min-length thresholds.

All thresholds, patterns, and POS filters are config-driven via
``knowledge_map.keywords.*`` — no hardcoded stop-word lists.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from ..config import get_nested

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class KeywordExtractor(Protocol):
    """Protocol for keyword extraction backends."""

    def extract(self, text: str) -> list[str]: ...


def create_keyword_extractor(config: dict[str, Any]) -> KeywordExtractor:
    """
    Create the best available keyword extractor.

    Tries SudachiPy first; falls back to regex if not installed.
    """
    kw_cfg = get_nested(config, "knowledge_map.keywords", {})

    try:
        return _SudachiExtractor(kw_cfg)
    except ImportError:
        logger.debug("SudachiPy not installed — using regex keyword extractor")
        return _RegexExtractor(kw_cfg)


# ---------------------------------------------------------------------------
# Backend A: SudachiPy (high precision)
# ---------------------------------------------------------------------------

_KANA_RE = re.compile(r"[\u3040-\u30FF]")
"""Matches any Hiragana or Katakana character — the signal for Japanese text."""


class _SudachiExtractor:
    """
    Morphological analysis via SudachiPy → POS filter.

    Language routing per text:
      - Text contains kana (ひらがな/カタカナ) → SudachiPy (Japanese)
      - Text has no kana (pure Chinese/Korean/other) → delegates to regex
        fallback, because SudachiPy is a Japanese-only analyzer.

    Config keys used (all under ``knowledge_map.keywords``):
      - pos_keep:       list of POS major categories to keep (default: ["名詞"])
      - sudachi_mode:   "A" | "B" | "C" (default: "C" = coarsest, compound nouns)
      - latin_min_len:  minimum length for Latin words (default: 3)
      - extra_stop_words: additional words to exclude
    """

    def __init__(self, kw_cfg: dict[str, Any]) -> None:
        from sudachipy import Dictionary

        self._tokenizer = Dictionary().create()

        mode_str = str(kw_cfg.get("sudachi_mode", "C")).upper()
        mode_map = {"A": self._tokenizer.SplitMode.A,
                     "B": self._tokenizer.SplitMode.B,
                     "C": self._tokenizer.SplitMode.C}
        self._mode = mode_map.get(mode_str, self._tokenizer.SplitMode.C)

        self._pos_keep: set[str] = set(kw_cfg.get("pos_keep", ["名詞"]))
        self._latin_min: int = int(kw_cfg.get("latin_min_len", 3))
        self._stop: set[str] = set(kw_cfg.get("extra_stop_words", []))

        # Regex fallback for non-Japanese text
        self._regex_fallback = _RegexExtractor(kw_cfg)

    def extract(self, text: str) -> list[str]:
        # No kana → not Japanese → delegate to regex (better for zh/ko/en)
        if not _KANA_RE.search(text):
            return self._regex_fallback.extract(text)

        keywords: list[str] = []
        seen: set[str] = set()

        # --- CJK via SudachiPy: concatenate consecutive nouns into compounds ---
        tokens = self._tokenizer.tokenize(text, self._mode)
        compound_parts: list[str] = []

        def _flush_compound() -> None:
            if not compound_parts:
                return
            compound = "".join(compound_parts)
            compound_parts.clear()
            # Skip single-char fragments and pure digits
            if len(compound) < 2 or compound.isdigit():
                return
            if compound not in self._stop and compound not in seen:
                keywords.append(compound)
                seen.add(compound)

        for tok in tokens:
            surface = tok.surface()
            pos_major = tok.part_of_speech()[0]  # e.g. "名詞"

            if pos_major in self._pos_keep:
                compound_parts.append(surface)
            else:
                _flush_compound()
        _flush_compound()

        # --- Latin words via regex (SudachiPy doesn't tokenize English well) ---
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", text):
            if len(word) >= self._latin_min and word not in self._stop:
                if word not in seen:
                    keywords.append(word)
                    seen.add(word)

        return keywords


# ---------------------------------------------------------------------------
# Backend B: Regex fallback (zero external dependencies)
# ---------------------------------------------------------------------------

class _RegexExtractor:
    """
    Improved regex-based extraction with configurable strip patterns.

    Config keys used (all under ``knowledge_map.keywords``):
      - latin_min_len:       minimum length for Latin words (default: 3)
      - cjk_min_len:         minimum length for raw CJK runs (default: 3)
      - cjk_strip_patterns:  list of regex patterns to strip from CJK runs
      - cjk_min_after_strip: minimum length after stripping (default: 2)
      - extra_stop_words:    additional words to exclude
    """

    def __init__(self, kw_cfg: dict[str, Any]) -> None:
        self._latin_min: int = int(kw_cfg.get("latin_min_len", 3))
        self._cjk_min: int = int(kw_cfg.get("cjk_min_len", 3))
        self._cjk_min_after: int = int(kw_cfg.get("cjk_min_after_strip", 2))
        self._stop: set[str] = set(kw_cfg.get("extra_stop_words", []))

        # Compile strip patterns once
        raw_patterns: list[str] = kw_cfg.get("cjk_strip_patterns", [
            r"^[のはをがにでとも]+",
            r"に関する$",
            r"について$",
            r"における$",
            r"および$",
        ])
        self._strip_res: list[re.Pattern[str]] = []
        for p in raw_patterns:
            try:
                self._strip_res.append(re.compile(p))
            except re.error:
                logger.warning(f"Invalid cjk_strip_pattern ignored: {p}")

    def extract(self, text: str) -> list[str]:
        keywords: list[str] = []

        # --- Latin words ---
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", text):
            if len(word) >= self._latin_min and word not in self._stop:
                keywords.append(word)

        # --- CJK runs ---
        cjk_runs = re.findall(
            r"[\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]{2,}", text
        )
        for run in cjk_runs:
            # Apply strip patterns
            cleaned = run
            for pat in self._strip_res:
                cleaned = pat.sub("", cleaned)

            # Accept if long enough after stripping
            if len(cleaned) >= self._cjk_min_after and cleaned not in self._stop:
                keywords.append(cleaned)
            # Also accept original if long enough (preserves compound terms)
            elif len(run) >= self._cjk_min and run not in self._stop:
                keywords.append(run)

        return keywords
