"""
Unit tests for processing modes (fast / balanced / best).

A mode is a scenario preset that expands to a set of config overrides, applied
BELOW the user's own overrides. This pins the contract:

  1. balanced (and None) == empty preset == today's defaults → backward compatible
  2. fast / best expand to the documented knob set (engine, parallelism, triage,
     figure-Vision, batching)
  3. unknown mode fails loud (ValueError), never silently runs at the wrong point
  4. user config_overrides WIN over the mode preset (advanced fine-tuning)
  5. the mode × file-type split is free: `fast` sets engine=vision_only, which the
     parser layer already routes per format (PDF vision_only, Office → Docling)

Pure config resolution — no parsing, no network. Fast and offline.

Run:
    python tests/unit/test_modes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docingest.api import _resolve_mode, _MODE_PRESETS, build_config
from docingest.config import get_nested


def test_balanced_is_noop() -> None:
    # balanced and None both mean "today's defaults" — empty override set.
    assert _resolve_mode("balanced") == {}
    assert _resolve_mode(None) == {}
    # And the built config keeps the stock engine (not overridden to anything).
    assert get_nested(build_config(mode="balanced"), "parsing.engine") == "docling"
    assert get_nested(build_config(mode=None), "parsing.engine") == "docling"
    print("  ✓ test_balanced_is_noop")


def test_fast_expands_to_documented_knobs() -> None:
    c = build_config(mode="fast")
    # engine=vision_only → PDF skips Docling, Office auto-delegates (per-format
    # split handled by the parser, not here).
    assert get_nested(c, "parsing.engine") == "vision_only"
    # high concurrency
    assert get_nested(c, "performance.parallel_files") == 64
    # Office figure-Vision OFF (the big slide-deck cost)
    for fmt in ("pptx", "docx", "xlsx"):
        assert get_nested(c, f"parsing.{fmt}.image_extraction.vision_enrich") is False, fmt
    # aggressive triage (skip MORE plain pages) — but garble nets stay ON
    assert get_nested(c, "parsing.vision.triage.min_text_length") == 20
    assert get_nested(c, "parsing.vision.triage.table_line_threshold") == 100
    assert get_nested(c, "parsing.vision.triage.enabled") is True, "fast must NOT disable triage"
    print("  ✓ test_fast_expands_to_documented_knobs")


def test_best_sends_every_page() -> None:
    c = build_config(mode="best")
    # triage off → every page to Vision (zero skip risk)
    assert get_nested(c, "parsing.vision.triage.enabled") is False
    # batching off → xlsx per-sheet (batch drops chart visuals)
    assert get_nested(c, "parsing.vision.batched_call.enabled") is False
    # best keeps the stock engine (no vision_only — quality path)
    assert get_nested(c, "parsing.engine") == "docling"
    print("  ✓ test_best_sends_every_page")


def test_unknown_mode_fails_loud() -> None:
    for bad in ("turbo", "FAST", "quality", ""):
        try:
            _resolve_mode(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for mode={bad!r}")
    print("  ✓ test_unknown_mode_fails_loud")


def test_user_override_wins_over_mode() -> None:
    # fast sets engine=vision_only; an explicit override must beat it...
    c = build_config(mode="fast", config_overrides={"parsing.engine": "docling"})
    assert get_nested(c, "parsing.engine") == "docling", "user override must win"
    # ...while the rest of the mode preset still applies.
    assert get_nested(c, "performance.parallel_files") == 64, "mode preset must survive"
    print("  ✓ test_user_override_wins_over_mode")


def test_preset_returns_fresh_dict() -> None:
    # _resolve_mode must not hand out the shared preset (callers merge into it).
    a = _resolve_mode("fast")
    a["parsing.engine"] = "tampered"
    b = _resolve_mode("fast")
    assert b["parsing.engine"] == "vision_only", "preset leaked a mutable reference"
    print("  ✓ test_preset_returns_fresh_dict")


def test_preset_names_are_the_three() -> None:
    assert set(_MODE_PRESETS) == {"fast", "balanced", "best"}, _MODE_PRESETS.keys()
    print("  ✓ test_preset_names_are_the_three")


def main() -> int:
    print("=== test_modes ===")
    for t in [
        test_balanced_is_noop,
        test_fast_expands_to_documented_knobs,
        test_best_sends_every_page,
        test_unknown_mode_fails_loud,
        test_user_override_wins_over_mode,
        test_preset_returns_fresh_dict,
        test_preset_names_are_the_three,
    ]:
        t()
    print("ALL processing-mode TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
