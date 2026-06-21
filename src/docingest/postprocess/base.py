"""
Post-processing abstraction — the shared skeleton for "second-pass" work
over a knowledge base's produced artefacts (sources/*.md, chunks.jsonl).

Why this exists:
    refine / graph / export / extraction each re-implemented the same loop —
    read artefacts, split when too big for the context window, call an LLM in
    parallel, merge, isolate per-unit errors. This module factors that loop
    out ONCE (the Runner) so a new post-processor only writes three hooks:

        prepare(config)        — one-time setup (e.g. compile a schema)
        process_unit(text)     — turn one piece of text into a partial result
        merge(results)         — combine partials of the same unit into one

    The Runner owns everything generic: context-budget splitting (a unit too
    large for one LLM call is split and processed in MULTIPLE parallel calls,
    then merged), a thread pool, per-unit error isolation, and a progress
    callback. extract is the first implementation; translate / classify /
    qa_gen would each be one more PostProcessor with the same three hooks.

This is intentionally the MINIMAL viable abstraction (one template-driven
extractor exists today). The hook shape is chosen so a second processor can
slot in without reshaping the Runner — but we don't pre-build machinery no
caller needs yet.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

from ..config import get_nested
from .source_loader import SourceUnit, load_units

logger = logging.getLogger(__name__)

# Partial-result type a processor produces per text piece.
P = TypeVar("P")


@dataclass
class UnitResult:
    """Outcome for one source unit (one md file / one chunk)."""

    unit_id: str
    source: str
    record: Any = None          # merged processor output (None on total failure)
    n_pieces: int = 1           # how many LLM calls this unit was split into
    n_ok: int = 0               # pieces that succeeded
    n_failed: int = 0           # pieces that failed (isolated, not fatal)
    errors: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    """Aggregate outcome of a post-processing run."""

    processor: str = ""
    units_total: int = 0
    units_ok: int = 0           # at least one piece succeeded
    units_failed: int = 0       # every piece failed
    pieces_total: int = 0
    pieces_failed: int = 0
    elapsed_ms: int = 0
    results: list[UnitResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class PostProcessor(ABC, Generic[P]):
    """A second-pass processor over knowledge-base artefacts.

    Implementations provide three hooks; the Runner supplies the loop.
    """

    #: Short stable name (used in CLI, config section, run reports).
    name: str = "postprocessor"

    @abstractmethod
    def prepare(self, config: dict[str, Any]) -> None:
        """One-time setup before any unit is processed (e.g. compile schema,
        build the system prompt). Called once per run."""

    @abstractmethod
    def process_piece(self, text: str, unit: SourceUnit, config: dict[str, Any]) -> P:
        """Process ONE piece of text (a whole unit, or one split of an
        oversized unit) into a partial result. May call an LLM. Raising is
        fine — the Runner isolates it as a failed piece."""

    @abstractmethod
    def merge(self, pieces: list[P], unit: SourceUnit, config: dict[str, Any]) -> Any:
        """Combine the partial results of a single unit's pieces into the
        unit's final record. For a single-piece unit, gets a 1-element list."""

    # -- optional hook: how big a piece may be (chars). Default reads config. --
    def piece_char_limit(self, config: dict[str, Any]) -> int:
        """Max characters per LLM call. A unit longer than this is split into
        multiple pieces processed in parallel. Conservative char proxy for the
        token budget (≈ chars/3 for CJK, chars/4 for Latin → well under the
        model context window at the default 30000)."""
        return int(get_nested(config, "postprocess.piece_char_limit", 30000))

    # -- optional hook: how to split an oversized unit into pieces. -----------
    def split(self, text: str, config: dict[str, Any]) -> list[str]:
        """Split a unit's text into pieces for separate LLM calls.

        Default: crude character splitting (extract uses this — it just needs
        the text to fit the window; field extraction tolerates a mid-table
        cut because merge dedups across pieces). Structure-sensitive
        processors override this to call split_on_headings() so tables/code
        are never cut. The Runner calls this once per unit; returning a
        single-element list means "don't split"."""
        limit = self.piece_char_limit(config)
        overlap = int(get_nested(config, "postprocess.piece_overlap", 1500))
        return _split_text(text, limit, overlap)


def _split_text(text: str, limit: int, overlap: int) -> list[str]:
    """Split into <=limit char pieces with overlap. limit<=0 → never split.

    The crude character splitter — used by processors that don't care about
    document structure (extract's default). Structure-aware processors
    (refine) use split_on_headings() below instead.
    """
    if limit <= 0 or len(text) <= limit:
        return [text]
    pieces: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = start + limit
        pieces.append(text[start:end])
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return pieces


def split_on_headings(content: str, target_tokens: int) -> list[str]:
    """
    Split markdown into heading-aligned pieces of ~target_tokens each, never
    cutting inside a protected span (table / code block / list / quote).

    Shared splitter for structure-sensitive post-processors. Lifted verbatim
    from refine's proven _split_for_refine so refine can delegate to it
    (removing that duplication) and any future processor that must preserve
    tables across a split gets the same behaviour.

    Greedy: accumulate lines until the running token estimate reaches the
    target, then close the piece at the NEXT heading boundary. A heading
    inside a protected span is not a valid cut point, so a table is never
    split across pieces. No usable heading boundary → a single piece.
    """
    # Imported here (not at module top) to avoid a chunkers→postprocess import
    # cycle risk and to keep base.py's import surface minimal.
    from ..chunkers.base import BaseChunker, find_protected_spans

    lines = content.split("\n")

    protected: set[int] = set()
    for start, end in find_protected_spans(lines):
        protected.update(range(start, end + 1))

    def is_heading(idx: int) -> bool:
        if idx == 0 or idx in protected:
            return False
        stripped = lines[idx].lstrip()
        return stripped.startswith("#") and " " in stripped

    pieces: list[str] = []
    seg_start = 0
    acc = 0
    for idx in range(len(lines)):
        acc += BaseChunker.estimate_tokens(lines[idx])
        if is_heading(idx) and acc >= target_tokens:
            piece = "\n".join(lines[seg_start:idx]).strip()
            if piece:
                pieces.append(piece)
            seg_start = idx
            acc = 0
    tail = "\n".join(lines[seg_start:]).strip()
    if tail:
        pieces.append(tail)
    return pieces


def run_pieces_parallel(
    fn: Callable[[Any], Any],
    items: list[Any],
    max_workers: int,
    *,
    parallel: bool = True,
) -> list[Any]:
    """
    Map ``fn`` over ``items``, in a thread pool when parallel and >1 item.

    The one place the "fan out per-piece LLM calls across a thread pool"
    pattern lives — shared by the Runner and by refine so the threading idiom
    isn't re-written per call site. Order is PRESERVED (uses ex.map, not
    as_completed) because callers that stitch pieces back together depend on
    input order. Exceptions propagate to the caller (callers decide isolation
    vs. fail) — the Runner wraps each call itself for per-piece isolation,
    while refine wants a raised error to fall through to its own handling.
    """
    if parallel and len(items) > 1:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
            return list(ex.map(fn, items))
    return [fn(it) for it in items]


class Runner:
    """Drives a PostProcessor over a knowledge base. Generic loop only — all
    processor-specific behaviour lives in the three hooks."""

    def __init__(self, processor: PostProcessor, config: dict[str, Any]):
        self.processor = processor
        self.config = config

    def run(
        self,
        knowledge_dir,
        *,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunResult:
        import time

        cfg = self.config
        self.processor.prepare(cfg)

        max_workers = int(get_nested(cfg, "postprocess.parallel", 8))

        units = list(load_units(knowledge_dir, cfg))
        result = RunResult(processor=self.processor.name, units_total=len(units))
        t0 = time.monotonic()

        for idx, unit in enumerate(units, 1):
            # The processor decides HOW to split (default: char-split; refine:
            # heading-aligned). One-element list = not split.
            pieces = self.processor.split(unit.text, cfg)
            ur = UnitResult(unit_id=unit.unit_id, source=unit.source, n_pieces=len(pieces))

            partials: list[Any] = []
            if len(pieces) == 1:
                partials, ur.n_failed, errs = self._process_pieces_serial(pieces, unit)
                ur.errors.extend(errs)
            else:
                # Oversized unit → MULTIPLE parallel LLM calls, then merge.
                partials, ur.n_failed, errs = self._process_pieces_parallel(
                    pieces, unit, max_workers
                )
                ur.errors.extend(errs)

            ur.n_ok = len(partials)
            result.pieces_total += len(pieces)
            result.pieces_failed += ur.n_failed

            if partials:
                try:
                    ur.record = self.processor.merge(partials, unit, cfg)
                except Exception as e:  # merge bug must not abort the run
                    ur.errors.append(f"merge failed: {e}")
                    ur.record = None

            if ur.record is not None and ur.n_ok > 0:
                result.units_ok += 1
                status = "ok"
            else:
                result.units_failed += 1
                status = "failed"

            result.results.append(ur)

            if on_progress is not None:
                try:
                    on_progress({
                        "current": idx,
                        "total": len(units),
                        "unit_id": unit.unit_id,
                        "status": status,
                        "pieces": len(pieces),
                        "failed_pieces": ur.n_failed,
                    })
                except Exception as e:
                    logger.warning(f"on_progress callback raised: {e}")

        result.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return result

    def _process_pieces_serial(self, pieces, unit):
        partials, n_failed, errs = [], 0, []
        for piece in pieces:
            try:
                partials.append(self.processor.process_piece(piece, unit, self.config))
            except Exception as e:
                n_failed += 1
                errs.append(str(e)[:200])
        return partials, n_failed, errs

    def _process_pieces_parallel(self, pieces, unit, max_workers):
        partials, n_failed, errs = [], 0, []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [
                ex.submit(self.processor.process_piece, p, unit, self.config)
                for p in pieces
            ]
            for fut in as_completed(futs):
                try:
                    partials.append(fut.result())
                except Exception as e:
                    n_failed += 1
                    errs.append(str(e)[:200])
        return partials, n_failed, errs
