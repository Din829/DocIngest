"""
Test the postprocess layer (template-driven extraction) — the parts that
need NO LLM: template → schema compilation, template validation, the
Runner's split / merge / error-isolation skeleton, and source loading.

The actual LLM extraction is covered by a real end-to-end run
(`docingest extract`), not here, so this suite stays fast and offline.

Run:
    python tests/unit/test_postprocess.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from pydantic import BaseModel

from docingest.postprocess.schema_builder import (
    load_template, build_schema, validate_template, field_types, TemplateError,
)
from docingest.postprocess.base import (
    PostProcessor, Runner, _split_text,
)
from docingest.postprocess.source_loader import load_units, _strip_frontmatter


_TEMPLATE = {
    "name": "test_card",
    "description": "test",
    "fields": [
        {"name": "title", "type": "str", "description": "the title"},
        {"name": "maybe", "type": "str", "description": "optional", "required": False},
        {"name": "items", "type": "list[str]", "description": "a list"},
    ],
    "rules": ["only extract what's present"],
}


# ---------------------------------------------------------------------------
# schema_builder
# ---------------------------------------------------------------------------

def test_build_schema_shape():
    print("=== test_build_schema_shape ===")
    schema = build_schema(_TEMPLATE)
    fields = schema.model_fields
    assert set(fields) == {"title", "maybe", "items"}, fields
    # required vs optional
    assert fields["title"].is_required()
    assert not fields["maybe"].is_required()
    print("  PASSED")


def test_build_schema_is_cached():
    """Same template name → identical class object (the POC's isinstance bug)."""
    print("=== test_build_schema_is_cached ===")
    a = build_schema(_TEMPLATE)
    b = build_schema(_TEMPLATE)
    assert a is b, "same template must yield the SAME class (cached)"
    # And an instance round-trips
    inst = a.model_validate({"title": "x", "maybe": None, "items": ["y"]})
    assert isinstance(inst, a)
    print("  PASSED")


def test_validate_template_rejects_bad():
    print("=== test_validate_template_rejects_bad ===")
    for bad in (
        {},                                              # no fields
        {"fields": []},                                  # empty fields
        {"fields": [{"type": "str"}]},                   # no name
        {"fields": [{"name": "a", "type": "nope"}]},     # bad type
        {"fields": [{"name": "a"}, {"name": "a"}]},      # dup name
    ):
        try:
            validate_template(bad)
            assert False, f"should have raised for {bad}"
        except TemplateError:
            pass
    print("  PASSED")


def test_load_builtin_template():
    print("=== test_load_builtin_template ===")
    root = Path(__file__).resolve().parent.parent.parent
    t = load_template(root / "postprocess_templates" / "doc_summary.yaml")
    assert t["name"] == "doc_summary"
    assert any(f["name"] == "key_entities" for f in t["fields"])
    print("  PASSED")


# ---------------------------------------------------------------------------
# base: split / merge / error isolation (no real LLM — a fake processor)
# ---------------------------------------------------------------------------

def test_split_text():
    print("=== test_split_text ===")
    assert _split_text("abc", 10, 2) == ["abc"]          # short → one piece
    assert _split_text("abc", 0, 2) == ["abc"]           # limit<=0 → never split
    pieces = _split_text("a" * 25, 10, 3)                 # 25 chars, 10 limit
    assert len(pieces) > 1
    assert all(len(p) <= 10 for p in pieces)
    print("  PASSED")


class _FakeRecord(BaseModel):
    title: str | None = None
    items: list[str] = []


class _FakeProcessor(PostProcessor):
    """Deterministic fake: no LLM. Piece text 'FAIL' raises (test isolation)."""
    name = "fake"

    def __init__(self, char_limit=1000):
        self._char_limit = char_limit

    def prepare(self, config):
        self.prepared = True

    def process_piece(self, text, unit, config):
        if "FAIL" in text:
            raise RuntimeError("boom")
        return _FakeRecord(title=text[:5], items=[text[:3]])

    def merge(self, pieces, unit, config):
        titles = [p.title for p in pieces if p.title]
        items = []
        for p in pieces:
            items.extend(p.items)
        return {"title": titles[0] if titles else None, "items": items}

    def piece_char_limit(self, config):
        return self._char_limit


def _kb_with_sources(texts: dict[str, str]) -> Path:
    """Make a temp knowledge base with sources/*.md."""
    d = Path(tempfile.mkdtemp(prefix="docingest_pp_test_"))
    src = d / "sources"
    src.mkdir()
    for name, body in texts.items():
        (src / f"{name}.md").write_text(body, encoding="utf-8")
    return d


def test_runner_happy_path():
    print("=== test_runner_happy_path ===")
    kb = _kb_with_sources({"doc1": "hello world", "doc2": "second doc"})
    config = {"postprocess": {"input": "sources", "parallel": 2}}
    runner = Runner(_FakeProcessor(), config)
    res = runner.run(kb)
    assert res.units_total == 2, res.units_total
    assert res.units_ok == 2, res.units_ok
    assert res.units_failed == 0
    # both produced a record
    assert all(r.record is not None for r in res.results)
    print("  PASSED")


def test_runner_error_isolation():
    """A unit whose piece raises is marked failed but doesn't abort the run."""
    print("=== test_runner_error_isolation ===")
    kb = _kb_with_sources({"good": "fine text", "bad": "FAIL here"})
    config = {"postprocess": {"input": "sources", "parallel": 2}}
    res = Runner(_FakeProcessor(), config).run(kb)
    assert res.units_total == 2
    assert res.units_ok == 1, f"expected 1 ok, got {res.units_ok}"
    assert res.units_failed == 1
    print("  PASSED")


def test_runner_splits_large_unit():
    """A unit larger than piece_char_limit is split into multiple pieces."""
    print("=== test_runner_splits_large_unit ===")
    big = "x" * 50
    kb = _kb_with_sources({"big": big})
    config = {"postprocess": {"input": "sources", "parallel": 4, "piece_overlap": 0}}
    res = Runner(_FakeProcessor(char_limit=10), config).run(kb)
    assert res.units_total == 1
    # 50 chars / 10 = 5 pieces (overlap 0)
    assert res.results[0].n_pieces == 5, res.results[0].n_pieces
    assert res.units_ok == 1
    print("  PASSED")


def test_progress_callback_isolated():
    """A buggy on_progress callback never breaks the run."""
    print("=== test_progress_callback_isolated ===")
    kb = _kb_with_sources({"d": "text"})
    def boom(_): raise ValueError("callback bug")
    res = Runner(_FakeProcessor(), {"postprocess": {}}).run(kb, on_progress=boom)
    assert res.units_ok == 1  # run completed despite callback raising
    print("  PASSED")


# ---------------------------------------------------------------------------
# source_loader
# ---------------------------------------------------------------------------

def test_strip_frontmatter():
    print("=== test_strip_frontmatter ===")
    md = "---\ntitle: X\nformat: pdf\n---\n# Body\ncontent"
    body, meta = _strip_frontmatter(md)
    assert body == "# Body\ncontent", repr(body)
    assert meta["title"] == "X" and meta["format"] == "pdf"
    # no frontmatter passes through
    body2, meta2 = _strip_frontmatter("# Just body")
    assert body2 == "# Just body" and meta2 == {}
    print("  PASSED")


def test_load_units_sources_and_chunks():
    print("=== test_load_units_sources_and_chunks ===")
    kb = _kb_with_sources({"a": "---\ntitle: A\n---\nbody a"})
    # chunks file
    (kb / "chunks.jsonl").write_text(
        json.dumps({"id": "c1", "text": "chunk text", "metadata": {"source": "s", "tokens": 100}}) + "\n",
        encoding="utf-8",
    )
    src_units = list(load_units(kb, {"postprocess": {"input": "sources"}}))
    assert len(src_units) == 1 and src_units[0].text.strip() == "body a"
    assert src_units[0].metadata.get("title") == "A"

    chunk_units = list(load_units(kb, {"postprocess": {"input": "chunks"}}))
    assert len(chunk_units) == 1 and chunk_units[0].unit_id == "c1"

    # min_chunk_tokens filter
    filtered = list(load_units(kb, {"postprocess": {"input": "chunks", "min_chunk_tokens": 500}}))
    assert len(filtered) == 0, "100-token chunk should be filtered by min 500"
    print("  PASSED")


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_build_schema_shape,
        test_build_schema_is_cached,
        test_validate_template_rejects_bad,
        test_load_builtin_template,
        test_split_text,
        test_runner_happy_path,
        test_runner_error_isolation,
        test_runner_splits_large_unit,
        test_progress_callback_isolated,
        test_strip_frontmatter,
        test_load_units_sources_and_chunks,
    ]
    for t in tests:
        t()
    print(f"\nALL {len(tests)} postprocess tests PASSED")


if __name__ == "__main__":
    main()
