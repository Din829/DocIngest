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
from pathlib import Path
from typing import Any

from .config import get_nested
from .models.provider import text_completion
from .chunkers.base import BaseChunker

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

    # 2. Package-bundled (relative to project root)
    package_root = Path(__file__).resolve().parent.parent.parent  # src/docingest → src → DocIngest
    bundled = package_root / skills_dir_name / f"{skill_name}.SKILL.md"
    if bundled.exists():
        return bundled.read_text(encoding="utf-8")

    raise FileNotFoundError(
        f"SKILL not found: {skill_name}.SKILL.md "
        f"(searched: {local}, {bundled})"
    )


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

    Args:
        source_path: Path to the source .md file (in sources/).
        output_dir:  Base output directory (e.g. knowledge/).
        config:      Full config dict.
        skill_name:  SKILL to use (None → default from config).

    Returns:
        dict with keys: source, output, tokens_in, tokens_out, skipped, warning
    """
    result = {
        "source": str(source_path),
        "output": "",
        "tokens_in": 0,
        "tokens_out": 0,
        "skipped": False,
        "warning": "",
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

    # Token check
    max_input = get_nested(config, "refine.max_input_tokens", 8000)
    tokens_in = BaseChunker.estimate_tokens(content)
    result["tokens_in"] = tokens_in

    if tokens_in > max_input:
        result["skipped"] = True
        result["warning"] = (
            f"Too large: {tokens_in:,} tokens > max_input_tokens={max_input:,}. "
            f"Skipped. Increase refine.max_input_tokens or refine manually."
        )
        logger.warning(f"Refine skipped {source_path.name}: {result['warning']}")
        return result

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
    max_output = get_nested(config, "refine.max_output_tokens", 8000)

    try:
        refined = text_completion(
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

    # Write output
    readable_dir_name = get_nested(config, "refine.output_dir", "readable")
    readable_dir = output_dir / readable_dir_name
    readable_dir.mkdir(parents=True, exist_ok=True)

    output_path = readable_dir / source_path.name
    output_path.write_text(refined, encoding="utf-8")

    result["output"] = str(output_path)
    result["tokens_out"] = BaseChunker.estimate_tokens(refined)

    logger.info(
        f"Refined: {source_path.name} → {readable_dir_name}/{source_path.name} "
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
) -> list[dict[str, Any]]:
    """
    Refine multiple Markdown files.

    Args:
        source_paths: List of .md files to refine.
        config:       Full config dict.
        output_dir:   Base output directory. If None, inferred from config.
        skill_name:   SKILL override (None → default).

    Returns:
        List of result dicts (one per file).
    """
    if output_dir is None:
        output_dir = Path(get_nested(config, "output.dir", "./knowledge"))

    results = []
    for path in source_paths:
        r = refine_single(path, output_dir, config, skill_name)
        results.append(r)

    return results
