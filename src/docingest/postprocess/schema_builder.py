"""
Template (YAML) → Pydantic schema compiler.

The declarative idea borrowed from Hyper-Extract: a template describes WHAT
to extract (field name / type / description / required) plus extraction
RULES, and we compile that into a Pydantic model the LLM is forced to fill
(structured output). Changing what gets extracted = editing YAML, no code.

A template looks like:

    name: doc_summary
    description: ...
    fields:
      - name: title
        type: str
        description: The document's main title
      - name: key_points
        type: list[str]
        description: Core takeaways, max 10
        required: false
    rules:
      - Only extract what the text states; never invent.

Type vocabulary is deliberately small (str / int / float / bool / list[str]).
Extraction targets a flat record per template — nested objects are out of
scope for v1 (keeps prompts simple and provider JSON-schema support solid).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, create_model


# YAML type string → Python type. Small on purpose; extend only when a real
# template needs it (no speculative types).
_TYPE_MAP: dict[str, Any] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list[str]": list[str],
}

LIST_TYPES = frozenset({"list[str]"})


class TemplateError(ValueError):
    """A template is malformed (missing fields, unknown type, etc.)."""


def load_template(path: str | Path) -> dict[str, Any]:
    """Read + validate a YAML template into a dict. Raises TemplateError on
    a malformed file so the caller fails loud at load time, not mid-run."""
    p = Path(path)
    if not p.exists():
        raise TemplateError(f"template not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise TemplateError(f"template is not valid YAML ({p}): {e}") from e
    validate_template(data, source=str(p))
    return data


def validate_template(template: dict[str, Any], *, source: str = "<dict>") -> None:
    """Fail loud on a malformed template — boundary validation (the template
    is user input), per the project's fail-loud-at-boundaries principle."""
    if not isinstance(template, dict):
        raise TemplateError(f"template must be a mapping ({source})")
    fields = template.get("fields")
    if not isinstance(fields, list) or not fields:
        raise TemplateError(f"template needs a non-empty 'fields' list ({source})")
    seen: set[str] = set()
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            raise TemplateError(f"fields[{i}] must be a mapping ({source})")
        name = f.get("name")
        if not name or not isinstance(name, str):
            raise TemplateError(f"fields[{i}] missing a string 'name' ({source})")
        if name in seen:
            raise TemplateError(f"duplicate field name {name!r} ({source})")
        seen.add(name)
        ftype = f.get("type", "str")
        if ftype not in _TYPE_MAP:
            raise TemplateError(
                f"field {name!r}: unknown type {ftype!r}. "
                f"Supported: {sorted(_TYPE_MAP)} ({source})"
            )


# Compiled-schema cache, keyed by template name. create_model() makes a NEW
# class every call, so without this the same template would yield distinct
# classes — wasteful AND breaks isinstance identity across calls (a bug the
# POC surfaced). One template name → one schema class for the process.
_SCHEMA_CACHE: dict[str, type[BaseModel]] = {}


def build_schema(template: dict[str, Any]) -> type[BaseModel]:
    """
    Compile a validated template into a Pydantic model class (cached).

    Required fields use Field(...) (no default); optional fields become
    ``T | None`` defaulting to None so the LLM can legitimately leave them
    empty rather than hallucinating a value.
    """
    name = str(template.get("name", "ExtractedRecord"))
    cached = _SCHEMA_CACHE.get(name)
    if cached is not None:
        return cached

    fields: dict[str, tuple] = {}
    for f in template["fields"]:
        py_type = _TYPE_MAP[f.get("type", "str")]
        desc = f.get("description", "")
        if f.get("required", True):
            fields[f["name"]] = (py_type, Field(..., description=desc))
        else:
            fields[f["name"]] = (py_type | None, Field(default=None, description=desc))

    model_name = name.title().replace("_", "").replace("/", "") or "ExtractedRecord"
    schema = create_model(model_name, **fields)  # type: ignore[call-overload]
    _SCHEMA_CACHE[name] = schema
    return schema


def field_types(template: dict[str, Any]) -> dict[str, str]:
    """Map field name → its YAML type string. Used by merge logic to decide
    list-merge vs scalar-first-wins."""
    return {f["name"]: f.get("type", "str") for f in template["fields"]}
