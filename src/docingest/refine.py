"""
Refine — AI-powered Markdown cleanup for human readability.

Standalone module, NOT part of the main pipeline.
Reads sources/*.md, calls LLM to clean up, writes to readable/*.md.
Originals are never modified.

Usage:
  from docingest.refine import refine_files
  refine_files([Path("knowledge/sources/spec.md")], config)
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import get_nested
from .models.provider import text_completion
from .chunkers.base import BaseChunker, find_protected_spans
from .utils.resources import resource_root

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SKILL loader
# ---------------------------------------------------------------------------

def _load_skill(skill_name: str, config: dict[str, Any]) -> str:
    """
    Load a .SKILL.md file as the system prompt for refine.

    Search order:
      1. Project-local skills/ directory
      2. Package-bundled skills/ directory (next to config/)
    """
    skills_dir_name = get_nested(config, "refine.skills_dir", "skills")

    # 1. Project-local (current working directory)
    local = Path.cwd() / skills_dir_name / f"{skill_name}.SKILL.md"
    if local.exists():
        return local.read_text(encoding="utf-8")

    # 2. Package-bundled (project root in dev, sys._MEIPASS when frozen)
    package_root = resource_root()
    bundled = package_root / skills_dir_name / f"{skill_name}.SKILL.md"
    if bundled.exists():
        return bundled.read_text(encoding="utf-8")

    raise FileNotFoundError(
        f"SKILL not found: {skill_name}.SKILL.md "
        f"(searched: {local}, {bundled})"
    )


def list_refine_skills(config: dict[str, Any]) -> list[dict[str, str]]:
    """List the refine SKILLs discoverable in the same two locations
    `_load_skill` searches: the project-local `skills/` dir (CWD) wins over
    the package-bundled one on a name clash.

    Each entry is `{name, summary, path}`. `summary` is the SKILL's first
    non-empty line — these files are bare prompts (no frontmatter), and the
    opening line states the role ("You are a document formatting
    specialist..."), which is enough for a consumer to pick a skill.

    Returned so `docingest skills list` and any programmatic consumer can
    discover the refine styles without reading the prompt bodies.
    """
    skills_dir_name = get_nested(config, "refine.skills_dir", "skills")
    package_root = resource_root()
    search_dirs = [Path.cwd() / skills_dir_name, package_root / skills_dir_name]

    found: dict[str, dict[str, str]] = {}
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for skill_md in sorted(directory.glob("*.SKILL.md")):
            name = skill_md.name[: -len(".SKILL.md")]
            if name in found:  # project-local already won
                continue
            summary = ""
            for line in skill_md.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    summary = line.strip()
                    break
            found[name] = {"name": name, "summary": summary, "path": str(skill_md)}
    return list(found.values())


# ---------------------------------------------------------------------------
# Large-file splitting — heading-aligned pieces for oversized inputs
# ---------------------------------------------------------------------------
# Why: a single LLM call on a very large document degrades badly — the model
# starts summarizing / rewriting tables into prose instead of preserving them
# (measured: a 100k-token doc fed whole keeps only ~9% of its table rows; split
# into ~8k-token pieces it keeps ~89%). So oversized inputs are cut on heading
# boundaries (never inside a protected table/code block), each piece refined
# independently with the SAME skill prompt, then stitched back in order.

def _split_for_refine(content: str, target_tokens: int) -> list[str]:
    """
    Split markdown into heading-aligned pieces of roughly ``target_tokens`` each.

    Greedy: accumulate lines until the running token count reaches the target,
    then close the piece at the NEXT heading boundary. A heading inside a
    protected span (table/code block, per ``find_protected_spans``) is not a
    valid cut point, so a table is never split across pieces. Falls back to a
    single piece when the document has no usable heading boundaries.
    """
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


def _refine_pieces(
    pieces: list[str],
    system_prompt: str,
    model_config: dict[str, Any],
    max_output: int,
    parallel: bool,
    max_workers: int,
) -> tuple[str, bool]:
    """
    Refine each piece with the same skill, then stitch back in order.

    Returns (stitched_markdown, any_truncated). Each piece reuses the exact
    same single-call path as a small file (text_completion → one-shot
    truncation retry handled inside). A piece that returns empty falls back to
    its original text so no content is silently dropped.
    """
    def _one(piece: str) -> tuple[str, bool]:
        refined, finish = text_completion(
            prompt=piece,
            system_prompt=system_prompt,
            model_config=model_config,
            max_tokens=max_output,
        )
        if not refined.strip():
            # Keep the original piece rather than dropping content.
            return piece, False
        return refined, finish == "length"

    if parallel and len(pieces) > 1:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
            outs = list(ex.map(_one, pieces))
    else:
        outs = [_one(p) for p in pieces]

    stitched = "\n\n".join(o[0] for o in outs)
    any_trunc = any(o[1] for o in outs)
    return stitched, any_trunc


# ---------------------------------------------------------------------------
# Cost pre-check — refine calls a paid LLM per piece; a large file fans out
# into many calls. Estimate the spend BEFORE any call and gate on it, the same
# off/warn/strict pattern `docingest run` uses for Vision cost. This is a
# system-boundary check (user-triggered, real money), not internal defense.
# ---------------------------------------------------------------------------

def estimate_refine_cost(files_or_pieces: list[tuple[str, int]], config: dict[str, Any]) -> dict[str, Any]:
    """
    Estimate the LLM spend for a refine run, BEFORE making any call.

    Refine cost is per-PIECE (one text_completion call each), NOT per-page —
    so it can't reuse safety.estimate_file_cost_usd (which is Vision/page
    based). We compute pieces ourselves and price each call from the SAME
    ``safety.vision_price_per_call`` table, looked up by the refine model.

    Args:
        files_or_pieces: list of (filename, token_count) for each source file.
        config: full config dict.

    Returns:
        {total_pieces, total_calls, est_cost_usd, per_file: [{file, tokens,
         pieces}], model}
    """
    from .safety import _lookup_vision_price  # same price table, refine model

    max_input = get_nested(config, "refine.max_input_tokens", 50000)
    target = get_nested(config, "refine.split_target_tokens", 8000)
    model_key = get_nested(config, "refine.model", "chunking_assist")
    refine_model = get_nested(config, f"models.{model_key}.primary.model", "")

    # Reuse the price table but key it on the refine model, not the vision one.
    # _lookup_vision_price reads models.vision.primary.model, so present the
    # refine model to it via a shallow overlay (real config is not mutated).
    overlay = {
        **config,
        "models": {
            **(get_nested(config, "models", {}) or {}),
            "vision": {"primary": {"model": refine_model}},
        },
    }
    price_per_call = _lookup_vision_price(overlay)

    per_file = []
    total_pieces = 0
    for name, tokens in files_or_pieces:
        if tokens > max_input:
            # ceil division — same greedy piece count the splitter targets
            pieces = max(1, -(-tokens // target))
        else:
            pieces = 1
        total_pieces += pieces
        per_file.append({"file": name, "tokens": tokens, "pieces": pieces})

    return {
        "total_pieces": total_pieces,
        "total_calls": total_pieces,
        "est_cost_usd": round(total_pieces * price_per_call, 4),
        "per_file": per_file,
        "model": refine_model,
        "price_per_call": price_per_call,
    }


def check_refine_budget(estimate: dict[str, Any], config: dict[str, Any]) -> tuple[str, list[str]]:
    """
    Decide what to do given a cost estimate, per ``refine.cost_check.mode``.

    Returns (action, reasons):
      action ∈ {"ok", "warn", "block"}.
        "ok"    → under budget (or mode=off); proceed silently.
        "warn"  → over budget but mode=warn (or strict-but-acknowledged
                   handled by caller); proceed after surfacing reasons.
        "block" → over budget AND mode=strict; caller must NOT proceed until
                   the user acknowledges.
      reasons → human-readable lines explaining what tripped (empty when ok).
    """
    mode = get_nested(config, "refine.cost_check.mode", "warn")
    if mode == "off":
        return "ok", []

    max_cost = get_nested(config, "refine.cost_check.max_est_cost_usd", None)
    max_pieces = get_nested(config, "refine.cost_check.max_pieces", None)

    reasons: list[str] = []
    if max_cost is not None and estimate["est_cost_usd"] > max_cost:
        reasons.append(
            f"預想コスト ${estimate['est_cost_usd']:.4f} > 上限 ${max_cost}"
        )
    if max_pieces is not None and estimate["total_pieces"] > max_pieces:
        reasons.append(
            f"分割数 {estimate['total_pieces']} > 上限 {max_pieces}"
        )

    if not reasons:
        return "ok", []
    return ("block" if mode == "strict" else "warn"), reasons


# ---------------------------------------------------------------------------
# Single file refine
# ---------------------------------------------------------------------------

def refine_single(
    source_path: Path,
    output_dir: Path,
    config: dict[str, Any],
    skill_name: str | None = None,
) -> dict[str, Any]:
    """
    Refine a single Markdown file for human readability.

    Files larger than ``refine.max_input_tokens`` are NOT skipped: they are
    split on heading boundaries into ~``refine.split_target_tokens`` pieces,
    each refined with the same skill (in parallel by default), then stitched
    back in order. A single huge LLM call degrades table fidelity badly;
    splitting keeps it high (measured ~89% table / ~98% fact fidelity vs ~9%
    when fed whole). Small files take the original single-call path unchanged.

    Args:
        source_path: Path to the source .md file (in sources/).
        output_dir:  Base output directory (e.g. knowledge/).
        config:      Full config dict.
        skill_name:  SKILL to use (None → default from config).

    Returns:
        dict with keys: source, output, tokens_in, tokens_out, skipped,
        warning, pieces (number of pieces refined; 1 = not split)
    """
    result = {
        "source": str(source_path),
        "output": "",
        "tokens_in": 0,
        "tokens_out": 0,
        "skipped": False,
        "warning": "",
        "pieces": 1,        # >1 when the file was large enough to be split
    }

    # Read source
    try:
        md_text = source_path.read_text(encoding="utf-8")
    except Exception as e:
        result["skipped"] = True
        result["warning"] = f"Cannot read: {e}"
        return result

    # Strip frontmatter for token counting (don't send YAML header to LLM)
    content = md_text
    frontmatter = ""
    if md_text.startswith("---"):
        end = md_text.find("---", 3)
        if end != -1:
            frontmatter = md_text[:end + 3]
            content = md_text[end + 3:].strip()

    # Token check. Fallback aligned with config/default.yaml's refine.max_input_tokens
    # so a misconfigured deployment matches the documented default instead of
    # silently skipping files at the old 8000 ceiling.
    max_input = get_nested(config, "refine.max_input_tokens", 50000)
    tokens_in = BaseChunker.estimate_tokens(content)
    result["tokens_in"] = tokens_in

    # Oversized inputs are split into heading-aligned pieces and refined in
    # parallel (see _split_for_refine / _refine_pieces), instead of being
    # skipped. A single huge LLM call degrades table fidelity badly; splitting
    # keeps it high. needs_split flips the LLM-call path below.
    needs_split = tokens_in > max_input

    if not content.strip():
        result["skipped"] = True
        result["warning"] = "Empty content"
        return result

    # Load SKILL
    skill = skill_name or get_nested(config, "refine.default_skill", "refine_default")
    try:
        system_prompt = _load_skill(skill, config)
    except FileNotFoundError as e:
        result["skipped"] = True
        result["warning"] = str(e)
        return result

    # Call LLM (reuse existing model config)
    model_key = get_nested(config, "refine.model", "chunking_assist")
    model_config = get_nested(config, f"models.{model_key}", {})
    # Fallback aligned with config/default.yaml's refine.max_output_tokens
    # (provider ceiling 65536). Old 8000 fallback silently truncated long
    # refines when yaml config was missing; aligning to the same ceiling
    # means a misconfigured deployment behaves consistently with a default one.
    max_output = get_nested(config, "refine.max_output_tokens", 65536)

    try:
        if needs_split:
            # Large file: split on heading boundaries, refine pieces in
            # parallel with the SAME skill prompt, stitch back in order.
            split_target = get_nested(config, "refine.split_target_tokens", 8000)
            parallel = get_nested(config, "refine.split_parallel", True)
            workers = get_nested(config, "performance.parallel_files", 4)
            pieces = _split_for_refine(content, split_target)
            result["pieces"] = len(pieces)
            logger.info(
                f"Refine splitting {source_path.name}: {tokens_in:,} tokens "
                f"> {max_input:,} → {len(pieces)} pieces "
                f"(~{split_target:,} tok each, parallel={parallel})."
            )
            refined, truncated = _refine_pieces(
                pieces, system_prompt, model_config, max_output,
                parallel=parallel, max_workers=workers,
            )
            finish_reason = "length" if truncated else "stop"
        else:
            # Small file: single call. text_completion handles one-shot retry
            # on finish_reason=="length" when models.defaults.retry_on_truncation
            # is true (default). If still "length" after that, the retry budget
            # (models.defaults.retry_max_tokens) was also exhausted.
            refined, finish_reason = text_completion(
                prompt=content,
                system_prompt=system_prompt,
                model_config=model_config,
                max_tokens=max_output,
            )
    except Exception as e:
        result["skipped"] = True
        result["warning"] = f"LLM call failed: {e}"
        logger.warning(f"Refine failed for {source_path.name}: {e}")
        return result

    if not refined.strip():
        result["skipped"] = True
        result["warning"] = "LLM returned empty response"
        return result

    # Surface truncation when it survives the retry layer so users know
    # the rewritten text is still incomplete. We warn + append a marker
    # rather than failing — preserves what the LLM did produce while
    # making the truncation state obvious in the output file itself.
    if finish_reason == "length":
        result["warning"] = (
            f"Refine output was still truncated after retry; "
            f"increase refine.max_output_tokens (current={max_output:,}) "
            f"or models.defaults.retry_max_tokens."
        )
        logger.warning(
            f"Refine truncated for {source_path.name} even after retry: "
            f"max_output_tokens={max_output:,} was not enough."
        )
        refined = refined.rstrip() + "\n\n<!-- refine-truncated: output hit max_output_tokens -->\n"

    # Write output — subdirectory per skill to avoid overwrite
    readable_dir_name = get_nested(config, "refine.output_dir", "readable")
    # Strip "refine_" prefix for cleaner directory names. Use the RESOLVED
    # `skill` (which already fell back to refine.default_skill when skill_name
    # is None), not the raw skill_name argument — otherwise a knowledge base
    # configured with default_skill=refine_html but invoked without --skill
    # would land in readable/default/*.md instead of readable/html/*.html.
    skill_short = skill.removeprefix("refine_")
    readable_dir = output_dir / readable_dir_name / skill_short
    readable_dir.mkdir(parents=True, exist_ok=True)

    # Skill-name convention: any skill containing "html" emits .html; everything
    # else keeps the source's .md suffix. Lets users add refine_html_xxx variants
    # without touching code, while default / faithful keep their existing .md
    # behaviour byte-for-byte.
    if skill_short and "html" in skill_short:
        output_path = readable_dir / (source_path.stem + ".html")
    else:
        output_path = readable_dir / source_path.name
    output_path.write_text(refined, encoding="utf-8")

    result["output"] = str(output_path)
    result["tokens_out"] = BaseChunker.estimate_tokens(refined)

    logger.info(
        f"Refined: {source_path.name} → {readable_dir_name}/{skill_short}/{source_path.name} "
        f"(tokens: {tokens_in:,} → {result['tokens_out']:,})"
    )

    return result


# ---------------------------------------------------------------------------
# Batch refine
# ---------------------------------------------------------------------------

def refine_files(
    source_paths: list[Path],
    config: dict[str, Any],
    output_dir: Path | None = None,
    skill_name: str | None = None,
    acknowledge: bool = False,
) -> list[dict[str, Any]]:
    """
    Refine multiple Markdown files.

    Runs a cost pre-check before calling any LLM (refine is paid, and a large
    file fans out into many calls). Behaviour follows ``refine.cost_check.mode``:
      - off          → no check, run.
      - warn (default)→ over budget logs a warning, then runs.
      - strict       → over budget BLOCKS the run unless ``acknowledge=True``;
                       returns a single result dict with ``blocked=True`` and
                       the cost ``estimate`` so the caller (CLI / MCP / GUI) can
                       confirm with the user, then re-call with acknowledge=True.

    Args:
        source_paths: List of .md files to refine.
        config:       Full config dict.
        output_dir:   Base output directory. If None, inferred from config.
        skill_name:   SKILL override (None → default).
        acknowledge:  Set True to proceed past a strict cost block (the user
                      has seen and accepted the estimate).

    Returns:
        List of result dicts (one per file). When a strict block fires and is
        not acknowledged, returns a single ``{"blocked": True, "estimate": ...,
        "reasons": [...]}`` dict instead — no files are processed.
    """
    if output_dir is None:
        output_dir = Path(get_nested(config, "output.dir", "./knowledge"))

    # --- Cost pre-check (before any LLM call) ---
    files_tok: list[tuple[str, int]] = []
    for path in source_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        # Count tokens on the body only (frontmatter is stripped before the
        # LLM call), matching refine_single's own measurement.
        body = text
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                body = text[end + 3:].strip()
        files_tok.append((path.name, BaseChunker.estimate_tokens(body)))

    estimate = estimate_refine_cost(files_tok, config)
    action, reasons = check_refine_budget(estimate, config)

    if action == "block" and not acknowledge:
        logger.warning(
            f"Refine blocked by cost gate (strict): {'; '.join(reasons)}. "
            f"Re-run with acknowledge=True / --yes to proceed."
        )
        return [{
            "blocked": True,
            "estimate": estimate,
            "reasons": reasons,
        }]
    if action == "warn" or (action == "block" and acknowledge):
        logger.warning(
            f"Refine cost notice: est ${estimate['est_cost_usd']:.4f} "
            f"over {estimate['total_pieces']} piece(s) "
            f"[{'; '.join(reasons)}]"
            + (" — acknowledged, proceeding." if acknowledge else " — proceeding (warn).")
        )

    results = []
    for path in source_paths:
        r = refine_single(path, output_dir, config, skill_name)
        results.append(r)

    return results
