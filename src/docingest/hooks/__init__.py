"""
Format-specific hooks — lightweight extension points around Docling parsing.

Design (intentionally minimal):
  - Pre-parse hooks run BEFORE Docling. They can return a BytesIO stream to
    replace the original file content (e.g. DOCX OMML → LaTeX preprocessing).
  - Post-parse hooks run AFTER Docling in specific pipeline positions:
      * "post_parse" — right after Phase 1.3 page images, before Vision.
        Used to inject structured data that Vision should be aware of
        (e.g. PPTX chart data directly read from python-pptx).
      * "pre_write"  — right before Phase 2 markdown write. Used for
        metadata enrichment that doesn't affect Vision (e.g. exiftool).

  - Hooks are registered as plain functions in HOOK_REGISTRY, not discovered
    via entry_points. Keep the mechanism explicit and debuggable.
  - Each hook is self-contained: it reads its own config section, handles
    its own missing-dependency fallback, and never raises — errors become
    warnings so one bad hook can't break the pipeline.

Why not reuse parsers/chunkers pattern: parsers and chunkers are single
swappable strategies, while hooks are additive — multiple hooks may apply
to the same file. They're closer to middleware than strategies.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Literal

from ..parsers.base import ParseResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook type signatures
# ---------------------------------------------------------------------------

# Pre-parse hook: given a file path and config, optionally return a BytesIO
# stream that should be parsed by Docling INSTEAD of the original file.
# Return None to leave the original file untouched.
#
# Hook MUST NOT mutate the original file. Stream lifetime is owned by caller.
PreParseHook = Callable[[Path, dict[str, Any]], "BytesIO | None"]


# Post-parse hook: given a file path, the in-memory ParseResult, and config,
# mutate parse_result in place (metadata, markdown, pages — whatever the hook
# is meant to enrich). Return nothing.
#
# A hook that detects up-front it has no work to do (disabled by config,
# missing binary, wrong document shape) SHOULD raise HookNoOp instead of
# quietly returning — the runner treats HookNoOp as "skip silently, do NOT
# record in the lineage transformations trail". Regular None-return means
# "I actually ran", which IS recorded.
PostParseHook = Callable[[Path, ParseResult, dict[str, Any]], None]


class HookNoOp(Exception):
    """
    Raised by a hook that decided it had no work to do.

    Distinct from a real failure: HookNoOp travels up silently (no
    warning log, no traceback) and keeps the hook out of the lineage
    transformations list. Use it from hooks whose behaviour is gated
    on config flags or external tool availability — so the provenance
    trail only records enrichments that actually changed anything.
    """


PostParsePhase = Literal["post_parse", "pre_write"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Keyed by matcher (file extension without dot, lowercase, OR "*" for all).
# Hooks are tried in the order they appear in each list.
#
# Runtime registration (via register_pre_parse_hook / register_post_parse_hook)
# is not exposed to keep the surface small. Default hooks are wired in
# _register_default_hooks below.
_PRE_PARSE_HOOKS: dict[str, list[PreParseHook]] = {}
_POST_PARSE_HOOKS: dict[PostParsePhase, dict[str, list[PostParseHook]]] = {
    "post_parse": {},
    "pre_write": {},
}


def _register_pre(extensions: list[str], hook: PreParseHook) -> None:
    for ext in extensions:
        _PRE_PARSE_HOOKS.setdefault(ext.lower(), []).append(hook)


def _register_post(
    phase: PostParsePhase,
    extensions: list[str],
    hook: PostParseHook,
) -> None:
    for ext in extensions:
        _POST_PARSE_HOOKS[phase].setdefault(ext.lower(), []).append(hook)


# ---------------------------------------------------------------------------
# Runner — called by pipeline.process_single_file
# ---------------------------------------------------------------------------

def run_pre_parse_hooks(
    file_path: Path,
    config: dict[str, Any],
) -> tuple[BytesIO | None, str | None]:
    """
    Run pre-parse hooks for the given file's format.

    Hooks run in registration order. The first hook that returns a non-None
    stream wins — subsequent hooks are skipped.

    Hooks must never raise: exceptions are caught and logged as warnings,
    and the pipeline falls through to the original file.

    Returns:
        (stream, hook_name).
          * stream    — BytesIO produced by the winning hook, or None if
                        no hook returned a stream.
          * hook_name — name of the winning hook (for lineage tracking),
                        or None when no hook produced a stream.
        Callers that don't care about lineage can simply unpack the first
        element and ignore the second.
    """
    ext = file_path.suffix.lstrip(".").lower()
    hooks = _PRE_PARSE_HOOKS.get(ext, []) + _PRE_PARSE_HOOKS.get("*", [])
    for hook in hooks:
        try:
            result = hook(file_path, config)
            if result is not None:
                logger.debug(
                    f"Pre-parse hook {hook.__name__} produced stream for {file_path.name}"
                )
                return result, hook.__name__
        except Exception as e:
            logger.warning(
                f"Pre-parse hook {hook.__name__} failed for {file_path.name}: {e}"
            )
    return None, None


def run_post_parse_hooks(
    file_path: Path,
    parse_result: ParseResult,
    config: dict[str, Any],
    phase: PostParsePhase,
) -> None:
    """
    Run post-parse hooks for the given phase + file format.

    All matching hooks run (no short-circuit). Hooks mutate parse_result
    in place. Exceptions are caught and logged so one bad hook can't
    break the pipeline.

    Each successfully-executed hook appends one entry to
    parse_result.transformations so downstream chunks carry a provenance
    trail of which enrichments ran. Failed hooks are NOT recorded —
    transformations is a positive trail, not a debug log.
    """
    ext = file_path.suffix.lstrip(".").lower()
    hooks = (
        _POST_PARSE_HOOKS[phase].get(ext, [])
        + _POST_PARSE_HOOKS[phase].get("*", [])
    )
    for hook in hooks:
        try:
            hook(file_path, parse_result, config)
        except HookNoOp:
            # Hook decided it had no work to do — skip silently, no
            # lineage entry, no warning. By convention this means the
            # hook's config flag is off or a prerequisite is missing.
            continue
        except Exception as e:
            logger.warning(
                f"Post-parse hook {hook.__name__} ({phase}) "
                f"failed for {file_path.name}: {e}"
            )
            continue
        parse_result.transformations.append({
            "step": "hook",
            "name": hook.__name__,
            "phase": phase,
        })


# ---------------------------------------------------------------------------
# Default hook registration
# ---------------------------------------------------------------------------
# Imports are deferred to avoid circular imports and to let each hook module
# handle its own optional-dependency checks at import time.

def _register_default_hooks() -> None:
    # #2 DOCX OMML → LaTeX preprocessing
    try:
        from .docx_omml import docx_omml_preprocess_hook
        _register_pre(["docx"], docx_omml_preprocess_hook)
    except ImportError as e:
        logger.debug(f"DOCX OMML hook not available: {e}")

    # #1 PPTX chart data direct-read (+ #10 shape reading order)
    try:
        from .pptx_chart import pptx_chart_hook
        _register_post("post_parse", ["pptx", "ppt"], pptx_chart_hook)
    except ImportError as e:
        logger.debug(f"PPTX chart hook not available: {e}")

    # XLSX AutoShape connector relationships — feeds Vision ground truth
    # for 画面遷移図 / フロー図 sheets where LibreOffice drops arrow
    # terminal points. Same shape as pptx_chart: structured_data → Vision.
    try:
        from .xlsx_connector import xlsx_connector_hook
        _register_post("post_parse", ["xlsx"], xlsx_connector_hook)
    except ImportError as e:
        logger.debug(f"XLSX connector hook not available: {e}")

    # #3 File metadata enrichment (Docling origin + exiftool + derived created)
    try:
        from .file_metadata import file_metadata_hook
        _register_post("pre_write", ["*"], file_metadata_hook)
    except ImportError as e:
        logger.debug(f"File metadata hook not available: {e}")

    # Derived aliases (title / exif.Title / filename → cleaned alias list).
    # Registered AFTER file_metadata_hook so exif.Title is available.
    try:
        from .derive_aliases import derive_aliases_hook
        _register_post("pre_write", ["*"], derive_aliases_hook)
    except ImportError as e:
        logger.debug(f"derive_aliases hook not available: {e}")

    # Derived tags stage 1 — format/lang. Stage 2 (keyword enrichment from
    # knowledge_map) runs AFTER the pipeline finishes its main loop, in
    # output/tags_enrichment.py, because keyword discrimination is a
    # corpus-wide signal not available at hook time.
    try:
        from .derive_tags import derive_tags_hook
        _register_post("pre_write", ["*"], derive_tags_hook)
    except ImportError as e:
        logger.debug(f"derive_tags hook not available: {e}")

    # Sensitive data sanitization (default OFF, must opt-in via config)
    try:
        from .sanitize import sanitize_hook
        _register_post("pre_write", ["*"], sanitize_hook)
    except ImportError as e:
        logger.debug(f"Sanitize hook not available: {e}")

    # Repeating header/footer stripper (default OFF, opt-in via config).
    # Removes per-page furniture (watermarks / logos / page numbers) that
    # Vision transcribes into the markdown. Registered LAST so it runs after
    # all content-enriching pre_write hooks have populated the markdown.
    try:
        from .strip_repeating import strip_repeating_hook
        _register_post("pre_write", ["*"], strip_repeating_hook)
    except ImportError as e:
        logger.debug(f"strip_repeating hook not available: {e}")


_register_default_hooks()
