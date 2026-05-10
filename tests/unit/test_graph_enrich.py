"""
Tests for the chunk enricher.

The enricher is pure replay over on-disk graph artefacts (no LLM calls)
so we can build complete fixtures: a fake graph/ directory + a tiny
chunks.jsonl + an inverted index by hand, and verify behaviour
deterministically.

Cardinal regression to guard:
    chunks.jsonl byte-for-byte unchanged across multiple enrich runs.
The whole point of this feature is that the original file is sacred;
if a future refactor accidentally rewrites it, this test fails loud.

Run:
    python tests/unit/test_graph_enrich.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


# ---------------------------------------------------------------------------
# Test fixture builder — produces a minimal but realistic kb directory.
# ---------------------------------------------------------------------------

def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _build_minimal_kb(tmp: Path) -> Path:
    """
    Lay out a synthetic knowledge base under tmp:

        kb/
        ├── chunks.jsonl              ← 3 chunks, one with the path-injection
        │                               header from the main pipeline so we
        │                               can verify enricher coexists with it.
        └── graph/
            ├── kv_store_text_chunks.json   ← LightRAG id <-> our chunk id
            └── vdb_entities.json            ← entity records, source_id'd
                                              into our chunks
    """
    kb = tmp / "kb"
    (kb / "graph").mkdir(parents=True)

    chunks = [
        {
            "id": "doc1_chunk_000",
            "text": (
                "[来源: sources/doc1.md > 章節1]\n"
                "## 章節1\n"
                "原状回復費用と敷金の関係について述べる。"
            ),
            "metadata": {
                "source": "sources/doc1.md",
                "original_file": "doc1.pdf",
                "title_path": "章節1",
                "format": "pdf",
                "language": "ja",
            },
        },
        {
            "id": "doc1_chunk_001",
            "text": (
                "[来源: sources/doc1.md > 章節2]\n"
                "## 章節2\n"
                "解約手続きは MY D-ROOM から行う。"
            ),
            "metadata": {
                "source": "sources/doc1.md",
                "original_file": "doc1.pdf",
                "title_path": "章節2",
                "format": "pdf",
                "language": "ja",
            },
        },
        {
            "id": "doc2_chunk_000",
            "text": (
                "## 単独の段落\n"
                "短い独立段落で path-injection 头なし。"
            ),
            "metadata": {
                "source": "sources/doc2.md",
                "original_file": "doc2.pdf",
                "title_path": "単独の段落",
                "format": "pdf",
                "language": "ja",
            },
        },
    ]
    chunks_path = kb / "chunks.jsonl"
    with open(chunks_path, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # LightRAG-style chunk id map.
    text_chunks = {
        "chunk-aaaaaaaaaaaa": {"full_doc_id": "doc1_chunk_000"},
        "chunk-bbbbbbbbbbbb": {"full_doc_id": "doc1_chunk_001"},
        "chunk-cccccccccccc": {"full_doc_id": "doc2_chunk_000"},
    }
    (kb / "graph" / "kv_store_text_chunks.json").write_text(
        json.dumps(text_chunks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Synthetic entities. Notice some span multiple chunks (<SEP>) and
    # some are exclusive to a single chunk; the enricher's selection
    # logic should prefer exclusive ones.
    entities = {
        "embedding_dim": 1536,
        "data": [
            {
                "__id__": "ent-001",
                "entity_name": "原状回復費用",
                "content": "原状回復費用\n退去時の修復に必要な費用。",
                "source_id": "chunk-aaaaaaaaaaaa",  # exclusive to chunk_000
                "file_path": "sources/doc1.md",
            },
            {
                "__id__": "ent-002",
                "entity_name": "敷金",
                "content": "敷金\n預かり金として扱われる費用。",
                "source_id": "chunk-aaaaaaaaaaaa",  # exclusive to chunk_000
                "file_path": "sources/doc1.md",
            },
            {
                "__id__": "ent-003",
                "entity_name": "MY D-ROOM",
                "content": "MY D-ROOM\n解約申込のオンラインプラットフォーム。",
                "source_id": "chunk-bbbbbbbbbbbb",  # exclusive to chunk_001
                "file_path": "sources/doc1.md",
            },
            {
                "__id__": "ent-004",
                "entity_name": "doc1.pdf",
                # Long doc-level entity — should be deprioritised by
                # the "shorter name first" tie-breaker even though it
                # touches multiple chunks.
                "content": "doc1.pdf\n文書全体を指す名称。",
                "source_id": "chunk-aaaaaaaaaaaa<SEP>chunk-bbbbbbbbbbbb",
                "file_path": "sources/doc1.md",
            },
            {
                "__id__": "ent-005",
                "entity_name": "段落",
                "content": "段落\n文章の単位。",
                "source_id": "chunk-cccccccccccc",
                "file_path": "sources/doc2.md",
            },
        ],
        "matrix": "",
    }
    (kb / "graph" / "vdb_entities.json").write_text(
        json.dumps(entities, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return kb


def _make_config(kb_path: Path, **overrides) -> dict:
    """Build a config dict mimicking what load_config would produce."""
    base: dict = {
        "output": {"dir": str(kb_path)},
        "graph": {
            "output_subdir": "graph",
            "input": {"chunks_file": "chunks.jsonl"},
            "enrich_chunks": {
                "enabled": True,
                "output_file": "chunks_enriched.jsonl",
                "max_entities_per_chunk": 5,
                "max_description_length": 100,
                "inject_into_text": True,
                "inject_into_metadata": True,
                "text_template": "[关键实体: {entities}]",
                "entity_separator": "; ",
                "name_desc_separator": " — ",
            },
        },
    }
    # Shallow override merge for tests; deep merge isn't needed at this
    # depth and keeping the helper trivial avoids leaking the production
    # deep_merge into test logic.
    for path, value in overrides.items():
        cur = base
        keys = path.split(".")
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        cur[keys[-1]] = value
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_enricher_basic_flow() -> None:
    """End-to-end: minimal kb → enriched file written, counts match."""
    from docingest.graph.enricher import enrich

    with tempfile.TemporaryDirectory() as tmp:
        kb = _build_minimal_kb(Path(tmp))
        config = _make_config(kb)

        result = enrich(kb, config)

        assert result.errors == [], f"unexpected errors: {result.errors}"
        assert result.chunks_total == 3
        assert result.chunks_enriched == 3       # all three chunks have hits
        assert result.chunks_unchanged == 0
        assert result.total_entities_injected > 0
        assert Path(result.written_path).name == "chunks_enriched.jsonl"

    print("OK: enricher basic flow + counts")


def test_chunks_jsonl_never_modified() -> None:
    """
    THE cardinal invariant: chunks.jsonl is sacred. Hash before / after.
    """
    from docingest.graph.enricher import enrich

    with tempfile.TemporaryDirectory() as tmp:
        kb = _build_minimal_kb(Path(tmp))
        config = _make_config(kb)

        chunks_path = kb / "chunks.jsonl"
        before = _md5(chunks_path)

        # Run twice — the second run is a regression risk if a future
        # change reads-back-then-rewrites instead of writing the sibling.
        enrich(kb, config)
        enrich(kb, config)

        after = _md5(chunks_path)
        assert before == after, "chunks.jsonl was modified by the enricher"

    print("OK: chunks.jsonl byte-for-byte preserved")


def test_text_injection_after_path_header() -> None:
    """
    Chunks that already have the main pipeline's '[来源:' header should
    get the entity line inserted RIGHT AFTER it (not at the very top).
    """
    from docingest.graph.enricher import enrich

    with tempfile.TemporaryDirectory() as tmp:
        kb = _build_minimal_kb(Path(tmp))
        config = _make_config(kb)
        enrich(kb, config)

        with open(kb / "chunks_enriched.jsonl", encoding="utf-8") as f:
            enriched = [json.loads(l) for l in f if l.strip()]

        # chunk_000 had a [来源: ...] header → entity line must be the
        # SECOND line of the enriched text.
        rec = next(r for r in enriched if r["id"] == "doc1_chunk_000")
        lines = rec["text"].split("\n")
        assert lines[0].startswith("[来源:"), f"path header lost: {lines[0]!r}"
        assert lines[1].startswith("[关键实体:"), (
            f"entity line not in second position: {lines[1]!r}"
        )

        # chunk doc2_chunk_000 had no [来源:] header → entity line must
        # be the FIRST line.
        rec2 = next(r for r in enriched if r["id"] == "doc2_chunk_000")
        first_line = rec2["text"].split("\n")[0]
        assert first_line.startswith("[关键实体:"), (
            f"entity line not at top when no path header: {first_line!r}"
        )

    print("OK: text injection respects path header position")


def test_metadata_entities_field_shape() -> None:
    """metadata.entities must be a list of {name, description, exclusive}."""
    from docingest.graph.enricher import enrich

    with tempfile.TemporaryDirectory() as tmp:
        kb = _build_minimal_kb(Path(tmp))
        config = _make_config(kb)
        enrich(kb, config)

        with open(kb / "chunks_enriched.jsonl", encoding="utf-8") as f:
            enriched = [json.loads(l) for l in f if l.strip()]

        rec = next(r for r in enriched if r["id"] == "doc1_chunk_000")
        ents = rec["metadata"]["entities"]
        assert isinstance(ents, list) and len(ents) >= 2
        for e in ents:
            assert set(e.keys()) == {"name", "description", "exclusive"}
            assert isinstance(e["exclusive"], bool)

        # Verify "原状回復費用" / "敷金" appear with exclusive=True (they
        # only touch chunk_000 in our fixture) and "doc1.pdf" is either
        # absent or marked non-exclusive.
        names = {e["name"] for e in ents}
        assert "原状回復費用" in names
        assert "敷金" in names
        for e in ents:
            if e["name"] == "doc1.pdf":
                assert e["exclusive"] is False

        # Existing metadata is preserved untouched.
        assert rec["metadata"]["source"] == "sources/doc1.md"
        assert rec["metadata"]["title_path"] == "章節1"
        assert "enriched_from" in rec["metadata"]

    print("OK: metadata.entities shape + original metadata preserved")


def test_topN_selection_prefers_exclusive() -> None:
    """
    With max_entities_per_chunk=1, each chunk should pick its exclusive
    entity over a multi-chunk one. doc1_chunk_000 has exclusive
    '敷金' (2 chars) and '原状回復費用' (6 chars) and shared 'doc1.pdf'
    (8 chars). Tie-breaker: exclusive first, then shorter name → '敷金'.
    """
    from docingest.graph.enricher import enrich

    with tempfile.TemporaryDirectory() as tmp:
        kb = _build_minimal_kb(Path(tmp))
        config = _make_config(kb, **{"graph.enrich_chunks.max_entities_per_chunk": 1})
        enrich(kb, config)

        with open(kb / "chunks_enriched.jsonl", encoding="utf-8") as f:
            enriched = [json.loads(l) for l in f if l.strip()]

        rec = next(r for r in enriched if r["id"] == "doc1_chunk_000")
        ents = rec["metadata"]["entities"]
        assert len(ents) == 1
        assert ents[0]["name"] == "敷金", f"top-1 picked {ents[0]['name']}"
        assert ents[0]["exclusive"] is True

    print("OK: top-N selection prefers exclusive + shorter")


def test_idempotent_re_run_replaces_not_stacks() -> None:
    """
    Running enrich twice should NOT produce two stacked '[关键实体:' lines.
    Second run replaces the first injection.
    """
    from docingest.graph.enricher import enrich

    with tempfile.TemporaryDirectory() as tmp:
        kb = _build_minimal_kb(Path(tmp))
        config = _make_config(kb)

        enrich(kb, config)
        first = (kb / "chunks_enriched.jsonl").read_text(encoding="utf-8")

        # Run a second time — output should be byte-identical (same input,
        # deterministic ordering).
        enrich(kb, config)
        second = (kb / "chunks_enriched.jsonl").read_text(encoding="utf-8")

        # Idempotent in content (modulo enriched_from timestamp which we
        # strip before compare).
        def _strip_ts(s: str) -> str:
            # Remove the enriched_from value, keep its key for shape.
            import re
            return re.sub(r'"enriched_from":"[^"]+"', '"enriched_from":"X"', s)

        assert _strip_ts(first) == _strip_ts(second), (
            "second run produced different output; enrichment is non-deterministic"
        )

        # Sanity: ensure the entity line never appears twice in any chunk.
        for line in second.splitlines():
            if line.strip():
                rec = json.loads(line)
                assert rec["text"].count("[关键实体:") <= 1, (
                    "stacked entity lines on re-run"
                )

    print("OK: re-running enrich replaces, doesn't stack")


def test_disabled_channels() -> None:
    """When inject_into_text=False, text must equal the source text."""
    from docingest.graph.enricher import enrich

    with tempfile.TemporaryDirectory() as tmp:
        kb = _build_minimal_kb(Path(tmp))
        config = _make_config(
            kb,
            **{
                "graph.enrich_chunks.inject_into_text": False,
                "graph.enrich_chunks.inject_into_metadata": True,
            },
        )
        enrich(kb, config)

        # Read original chunks and enriched, compare per-id texts.
        with open(kb / "chunks.jsonl", encoding="utf-8") as f:
            original = {json.loads(l)["id"]: json.loads(l)["text"]
                        for l in f if l.strip()}
        with open(kb / "chunks_enriched.jsonl", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                assert rec["text"] == original[rec["id"]], (
                    f"text changed despite inject_into_text=False for {rec['id']}"
                )
                # metadata channel still active
                if rec["id"] != "doc2_chunk_000":  # has hits
                    assert "entities" in rec["metadata"]

    print("OK: inject_into_text=False keeps text as-is")


def test_missing_graph_dir_returns_error_no_raise() -> None:
    """No graph/ dir → soft error in result, no exception."""
    from docingest.graph.enricher import enrich

    with tempfile.TemporaryDirectory() as tmp:
        kb = Path(tmp) / "kb"
        kb.mkdir()
        # chunks.jsonl exists, graph/ does NOT.
        (kb / "chunks.jsonl").write_text(
            json.dumps({"id": "x", "text": "y", "metadata": {}}) + "\n",
            encoding="utf-8",
        )
        config = _make_config(kb)

        result = enrich(kb, config)
        assert result.errors, "expected an error message"
        assert "graph dir not found" in result.errors[0]

    print("OK: missing graph dir surfaces as soft error")


def test_missing_chunks_file_returns_error() -> None:
    """No chunks.jsonl → soft error, no exception."""
    from docingest.graph.enricher import enrich

    with tempfile.TemporaryDirectory() as tmp:
        kb = Path(tmp) / "kb"
        (kb / "graph").mkdir(parents=True)
        config = _make_config(kb)

        result = enrich(kb, config)
        assert result.errors
        assert "chunks file not found" in result.errors[0]

    print("OK: missing chunks file surfaces as soft error")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> None:
    test_enricher_basic_flow()
    test_chunks_jsonl_never_modified()
    test_text_injection_after_path_header()
    test_metadata_entities_field_shape()
    test_topN_selection_prefers_exclusive()
    test_idempotent_re_run_replaces_not_stacks()
    test_disabled_channels()
    test_missing_graph_dir_returns_error_no_raise()
    test_missing_chunks_file_returns_error()
    print("\nAll graph-enrich tests passed.")


if __name__ == "__main__":
    main()
