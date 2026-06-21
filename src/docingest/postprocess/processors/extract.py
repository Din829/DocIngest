"""
Template-driven structured extraction — the first PostProcessor.

Given a YAML template (field table + rules), it forces an LLM to fill that
schema from each source unit and returns a strongly-typed record. Long units
are split by the Runner into multiple pieces; this processor's merge() folds
the per-piece partials back into one record:

  * scalar fields (str/int/float/bool) — first non-empty piece wins
    (key facts usually appear early; later pieces rarely override well).
  * list fields (list[str])            — union across pieces, order-preserved,
    deduplicated, then truncated to max_list_items (avoids the unbounded
    list growth the POC surfaced on a 48-piece document).

No state beyond the compiled schema + system prompt is kept, so the same
processor instance is safe to reuse across units.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ...config import get_nested
from ...models.provider import text_completion_structured
from ...utils.resources import resource_root
from ..base import PostProcessor
from ..schema_builder import build_schema, field_types, load_template, LIST_TYPES
from ..source_loader import SourceUnit


class ExtractProcessor(PostProcessor[BaseModel]):
    """Fill a template's schema from each source unit via structured output."""

    name = "extract"

    def __init__(self, template: dict[str, Any]):
        # template is already loaded + validated by the caller (api.run)
        self._template = template
        self._schema: type[BaseModel] | None = None
        self._system_prompt: str = ""
        self._field_types: dict[str, str] = {}
        self._model_config: dict[str, Any] | None = None
        self._max_list_items = 30

    # -- hook: one-time setup -------------------------------------------------
    def prepare(self, config: dict[str, Any]) -> None:
        self._schema = build_schema(self._template)
        self._field_types = field_types(self._template)
        self._system_prompt = _build_system_prompt(self._template)
        # Reuse the unified model config (models.defaults inherited) — the
        # extraction task gets its own optional override section but defaults
        # to the one-model-to-rule-them-all, like every other task.
        self._model_config = _resolve_model_config(config)
        self._max_list_items = int(
            get_nested(config, "postprocess.extract.max_list_items", 30)
        )

    # -- hook: process one piece of text -------------------------------------
    def process_piece(
        self, text: str, unit: SourceUnit, config: dict[str, Any]
    ) -> BaseModel:
        assert self._schema is not None  # prepare() always runs first
        user_prompt = f"### 待抽取文档片段：\n{text}"
        return text_completion_structured(
            prompt=user_prompt,
            response_schema=self._schema,
            system_prompt=self._system_prompt,
            model_config=self._model_config,
        )

    # -- hook: merge a unit's pieces -----------------------------------------
    def merge(
        self, pieces: list[BaseModel], unit: SourceUnit, config: dict[str, Any]
    ) -> dict[str, Any]:
        assert self._schema is not None
        merged: dict[str, Any] = {}
        for name, ftype in self._field_types.items():
            if ftype in LIST_TYPES:
                seen: set = set()
                out: list = []
                for piece in pieces:
                    for v in (getattr(piece, name, None) or []):
                        if v not in seen:
                            seen.add(v)
                            out.append(v)
                merged[name] = out[: self._max_list_items]
            else:
                val = None
                for piece in pieces:
                    v = getattr(piece, name, None)
                    if v not in (None, "", [], {}):
                        val = v
                        break
                merged[name] = val
        # Validate the merged dict back through the schema so the output is
        # guaranteed schema-conformant even after our manual field merge.
        return self._schema.model_validate(merged).model_dump()


def _build_system_prompt(template: dict[str, Any]) -> str:
    """Assemble the extraction system prompt from the template's rules."""
    rules = template.get("rules") or []
    rules_block = "\n".join(f"- {r}" for r in rules)
    desc = template.get("description", "")
    lines = [
        "你是一个专业的信息抽取助手。请严格按照给定的结构化 schema，"
        "从用户提供的文档片段中抽取信息。",
    ]
    if desc:
        lines.append(f"\n抽取目标：{desc}")
    if rules_block:
        lines.append(f"\n抽取规则：\n{rules_block}")
    return "\n".join(lines)


def _resolve_model_config(config: dict[str, Any]) -> dict[str, Any]:
    """Build the model_config for extraction.

    Precedence mirrors the rest of the pipeline: a dedicated
    models.extraction block wins if present; otherwise inherit
    models.defaults (one model for every task). The _defaults sub-dict is
    injected by load_config and carried through so resolve_max_tokens /
    resolve_max_retries see the global defaults.
    """
    models = config.get("models") or {}
    task_cfg = models.get("extraction")
    if isinstance(task_cfg, dict) and ("primary" in task_cfg or "provider" in task_cfg):
        cfg = dict(task_cfg)
    else:
        defaults = models.get("defaults") or {}
        cfg = {
            "primary": defaults.get("primary"),
            "fallback": defaults.get("fallback"),
        }
    # Carry the injected _defaults so token/retry resolution works.
    if "_defaults" not in cfg and isinstance(models.get("defaults"), dict):
        cfg["_defaults"] = models["defaults"]
    return cfg


# ---------------------------------------------------------------------------
# Template discovery — project-local dir wins over package-bundled (mirrors
# refine's SKILL lookup, so users add a template the same way they add a
# refine skill: drop a YAML in their project dir).
# ---------------------------------------------------------------------------

def resolve_template_path(name_or_path: str, config: dict[str, Any]) -> Path:
    """
    Resolve a --template argument to a YAML file path.

    Order:
      1. A direct path to an existing .yaml/.yml file → used as-is.
      2. Project-local: <cwd>/<template_dir>/<name>.yaml
      3. Package-bundled: <resource_root>/postprocess_templates/<name>.yaml
    """
    p = Path(name_or_path)
    if p.suffix.lower() in (".yaml", ".yml") and p.exists():
        return p

    dir_name = get_nested(
        config, "postprocess.extract.template_dir", "postprocess_templates"
    )
    stem = name_or_path
    if stem.endswith((".yaml", ".yml")):
        stem = Path(stem).stem

    local = Path.cwd() / dir_name / f"{stem}.yaml"
    if local.exists():
        return local

    bundled = resource_root() / "postprocess_templates" / f"{stem}.yaml"
    if bundled.exists():
        return bundled

    raise FileNotFoundError(
        f"extraction template not found: {name_or_path!r} "
        f"(searched: {local}, {bundled})"
    )
