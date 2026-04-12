"""
Content-based format detection (magika wrapper).

Problem
-------
DocIngest decides a file's format from `path.suffix`. This is correct
99% of the time and wrong in two important edge cases:

  1. **Weak or missing extensions**: files inside ZIP archives often
     lose their extensions (`Dockerfile`, `README`, `notes`) or use
     generic ones (`.bin`, `.dat`, `.tmp`). The suffix gives us nothing.

  2. **Renamed files**: a user downloads a PDF, saves it with a `.txt`
     extension, and feeds it to DocIngest. The suffix lies.

What we do
----------
Wrap Google's `magika` ML model behind a thin function:

    detect_format(file_path, config) -> str | None

It returns a **corrected format string** when it has high confidence and
the original suffix was weak, otherwise `None` — meaning "trust the
suffix". The caller keeps the suffix unless we return a correction.

Design commitments
------------------
* **Optional dependency** — `import magika` is done lazily inside the
  function. Missing → returns None → caller keeps suffix behaviour.
* **Conservative default** — `correct_strong_extensions` defaults to
  False. Strong suffixes like `.pdf` / `.docx` are kept even if magika
  disagrees, because user-maintained extensions are more reliable than
  ML guessing on Office subtypes.
* **Weak-extension detection is data-driven** — the list is configured
  via `parsing.magika.weak_extensions`. New edge cases are added in
  config, not code.
* **No path/content mutation** — this function only returns a string.
  It never renames files or rewrites suffixes. The caller decides what
  to do with the correction.
* **Singleton magika instance** — magika loads a ~25MB ML model. We
  cache one instance per process.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import get_nested

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default weak extensions (caller can override via config)
# ---------------------------------------------------------------------------

_DEFAULT_WEAK_EXTENSIONS: set[str] = {
    "",         # no extension at all (Dockerfile, README, Makefile, ...)
    "bin",
    "dat",
    "tmp",
    "out",
    "raw",
    "dump",
}


# ---------------------------------------------------------------------------
# Magika → DocIngest format label mapping
# ---------------------------------------------------------------------------
#
# Magika emits labels like "pdf", "docx", "python", "javascript",
# "markdown", etc. Most map 1:1 to DocIngest's format strings (which
# themselves are just extension names). For a handful we translate
# because magika uses a different name.
#
# If a magika label isn't in this map, it's used as-is — magika's
# labels are mostly DocIngest-compatible.

_MAGIKA_LABEL_TO_FORMAT: dict[str, str] = {
    # Text-ish files — DocIngest treats them all as plain text / code
    "python": "py",
    "javascript": "js",
    "typescript": "ts",
    "shell": "sh",
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "markdown": "md",
    "plaintext": "txt",
    # Office: magika's labels already match DocIngest (docx/pptx/xlsx/pdf)
}


# ---------------------------------------------------------------------------
# Singleton magika loader (lazy, one per process)
# ---------------------------------------------------------------------------

_magika_instance: Any = None
_magika_load_failed: bool = False


def _get_magika() -> Any:
    """
    Lazily load and cache the Magika instance. Returns None if magika
    is not installed or failed to initialise.
    """
    global _magika_instance, _magika_load_failed

    if _magika_instance is not None:
        return _magika_instance
    if _magika_load_failed:
        return None

    try:
        import magika  # type: ignore[import-not-found]
        _magika_instance = magika.Magika()
        return _magika_instance
    except ImportError:
        logger.debug(
            "magika not installed; content-based format detection disabled. "
            "pip install magika to enable."
        )
        _magika_load_failed = True
        return None
    except Exception as e:
        logger.warning(f"magika failed to initialise: {e}")
        _magika_load_failed = True
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_format(
    file_path: Path,
    config: dict[str, Any],
) -> str | None:
    """
    Decide whether the suffix-based format should be overridden by
    content-based detection.

    Returns:
        - A format string (e.g. "pdf", "py", "md") when magika has a
          confident verdict AND the suffix was weak (or the config asks
          us to correct strong suffixes).
        - None when we trust the suffix — caller keeps its existing
          behaviour.

    Config (all under parsing.magika):
        enabled                     — master toggle (default true when
                                      magika is installed)
        weak_extensions             — list of extensions to override
                                      unconditionally (default above)
        correct_strong_extensions   — if True, magika can override even
                                      strong extensions like .pdf/.docx
                                      (default False — conservative)
    """
    if not get_nested(config, "parsing.magika.enabled", True):
        return None

    magika_instance = _get_magika()
    if magika_instance is None:
        return None  # magika unavailable → keep suffix

    # Normalise the suffix to the same form DocIngest uses downstream
    # (lowercase, no leading dot).
    suffix = file_path.suffix.lstrip(".").lower()
    weak_extensions: set[str] = set(
        get_nested(
            config,
            "parsing.magika.weak_extensions",
            list(_DEFAULT_WEAK_EXTENSIONS),
        )
    )
    correct_strong = bool(
        get_nested(config, "parsing.magika.correct_strong_extensions", False)
    )

    is_weak = suffix in weak_extensions
    if not is_weak and not correct_strong:
        # Strong suffix and user didn't opt in to aggressive correction:
        # keep the suffix, no magika call needed.
        return None

    # Run magika. We let it handle file I/O itself; it's optimised for it.
    try:
        result = magika_instance.identify_path(str(file_path))
    except Exception as e:
        logger.debug(f"magika identify failed for {file_path.name}: {e}")
        return None

    # Different magika versions expose the label through different
    # attribute paths. Handle both layouts defensively.
    label = _extract_label(result)
    if not label:
        return None

    # Translate magika label to DocIngest format string.
    mapped = _MAGIKA_LABEL_TO_FORMAT.get(label, label)

    # If magika's verdict matches the suffix, don't bother correcting.
    if mapped == suffix:
        return None

    # For weak suffixes, always apply the correction.
    # For strong suffixes (only reached when correct_strong is True),
    # the correction also applies — the gate was `if not is_weak and not
    # correct_strong` above.
    logger.info(
        f"magika correction: {file_path.name} "
        f"suffix={suffix!r} → detected={mapped!r}"
    )
    return mapped


def _extract_label(magika_result: Any) -> str | None:
    """
    Pull a format label out of a Magika result object.

    Magika's output shape has changed between versions:
      0.5.x : result.output.ct_label
      0.6.x : result.prediction.output.label
      1.x+  : result.output.label  (via identify_path)
    We try each in turn and return the first non-empty string.
    """
    # Preferred: `result.output.label` (magika 1.x identify_path return)
    output = getattr(magika_result, "output", None)
    if output is not None:
        for attr in ("label", "ct_label"):
            value = getattr(output, attr, None)
            if value:
                return str(value)

    # Older: `result.prediction.output.label`
    prediction = getattr(magika_result, "prediction", None)
    if prediction is not None:
        pred_output = getattr(prediction, "output", None)
        if pred_output is not None:
            for attr in ("label", "ct_label"):
                value = getattr(pred_output, attr, None)
                if value:
                    return str(value)

    return None
