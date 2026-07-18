# -*- coding: utf-8 -*-
"""Description enrichment — retrieval-optimized frontmatter sentence.

Covers: default-off gate (no AI call), happy-path write-back via mocked
text_completion, skip of files that already carry a description
(idempotency across runs), malformed AI reply tolerance, and numeric-key
robustness (int and str keys both accepted).

Run:
    python -m pytest tests/unit/test_description_enrichment.py -q
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import docingest.models.provider as provider_mod
from docingest.output.description_enrichment import enrich_sources_with_descriptions


def _make_kb(descriptions_present: bool = False) -> tuple[Path, dict]:
    out = Path(tempfile.mkdtemp(prefix="docingest_desc_"))
    (out / "sources").mkdir(parents=True)
    files = []
    for name in ("alpha", "beta"):
        desc_line = "description: old text\n" if descriptions_present else ""
        (out / "sources" / f"{name}.md").write_text(
            f"---\nsource: {name}.md\ntitle: {name}\nformat: md\n{desc_line}---\n\n"
            f"# {name}\n\nBody of {name}.\n",
            encoding="utf-8",
        )
        files.append({
            "path": f"sources/{name}.md",
            "original": f"{name}.md",
            "format": "md",
            "language": "en",
            "keywords": [name, "body"],
        })
    return out, {"files": files}


def _config(enabled: bool = True) -> dict:
    return {
        "output": {"derived_metadata": {"description": {"enabled": enabled}}},
        "models": {"chunking_assist": {}},
    }


def _frontmatter_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text.split("---")[1]


def test_disabled_by_default_no_ai_call(monkeypatch):
    out, km = _make_kb()
    calls = []
    monkeypatch.setattr(
        provider_mod, "text_completion",
        lambda **kw: calls.append(1) or ("1: x\n2: y", "stop"),
    )
    assert enrich_sources_with_descriptions(km, out, _config(enabled=False)) == 0
    assert calls == [], "disabled pass must not call the LLM"


def test_writes_descriptions(monkeypatch):
    out, km = _make_kb()
    monkeypatch.setattr(
        provider_mod, "text_completion",
        lambda **kw: ("1: Alpha notes about testing.\n2: Beta reference sheet.", "stop"),
    )
    assert enrich_sources_with_descriptions(km, out, _config()) == 2
    assert "description: Alpha notes about testing." in _frontmatter_of(out / "sources" / "alpha.md")
    assert "description: Beta reference sheet." in _frontmatter_of(out / "sources" / "beta.md")


def test_existing_descriptions_skipped(monkeypatch):
    out, km = _make_kb(descriptions_present=True)
    calls = []
    monkeypatch.setattr(
        provider_mod, "text_completion",
        lambda **kw: calls.append(1) or ("1: new\n2: new", "stop"),
    )
    assert enrich_sources_with_descriptions(km, out, _config()) == 0
    assert calls == [], "files with descriptions must not be re-sent to the LLM"
    assert "description: old text" in _frontmatter_of(out / "sources" / "alpha.md")


def test_malformed_ai_reply_is_tolerated(monkeypatch):
    out, km = _make_kb()
    monkeypatch.setattr(
        provider_mod, "text_completion",
        lambda **kw: ("just some prose, not a mapping", "stop"),
    )
    assert enrich_sources_with_descriptions(km, out, _config()) == 0
    assert "description" not in _frontmatter_of(out / "sources" / "alpha.md")


def test_string_numeric_keys_accepted(monkeypatch):
    out, km = _make_kb()
    # Quoted keys parse as YAML strings instead of ints.
    monkeypatch.setattr(
        provider_mod, "text_completion",
        lambda **kw: ('"1": First file.\n"2": Second file.', "stop"),
    )
    assert enrich_sources_with_descriptions(km, out, _config()) == 2
