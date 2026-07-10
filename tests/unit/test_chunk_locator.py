"""Unified chunk locator contract and cache-replay regression tests."""

from __future__ import annotations

import json
from pathlib import Path

from docingest.api import ingest
from docingest.output.chunks_writer import build_chunk_id
from docingest.parsers.base import PAGEBREAK_MARKER
from docingest.pipeline import _attach_chunk_locators


def _views(*texts: str) -> list[tuple[str, dict]]:
    return [(text, {}) for text in texts]


def test_pdf_locators_cover_single_and_cross_page_chunks() -> None:
    views = _views(
        "page one",
        f"end one{PAGEBREAK_MARKER}start two",
        f"end two{PAGEBREAK_MARKER}start three",
        "page three",
    )

    warning = _attach_chunk_locators(views, doc_format="pdf", page_count=3)

    assert warning is None
    assert [metadata["locator"] for _, metadata in views] == [
        {"kind": "page", "start": 1, "end": 1},
        {"kind": "page", "start": 1, "end": 2},
        {"kind": "page", "start": 2, "end": 3},
        {"kind": "page", "start": 3, "end": 3},
    ]


def test_pdf_marker_at_chunk_edge_does_not_invent_cross_page_content() -> None:
    marker_at_end = _views(f"page one{PAGEBREAK_MARKER}", "page two")
    marker_at_start = _views("page one", f"{PAGEBREAK_MARKER}page two")

    assert _attach_chunk_locators(
        marker_at_end, doc_format="pdf", page_count=2
    ) is None
    assert _attach_chunk_locators(
        marker_at_start, doc_format="pdf", page_count=2
    ) is None

    expected = [
        {"kind": "page", "start": 1, "end": 1},
        {"kind": "page", "start": 2, "end": 2},
    ]
    assert [m["locator"] for _, m in marker_at_end] == expected
    assert [m["locator"] for _, m in marker_at_start] == expected


def test_cached_source_header_is_not_mistaken_for_page_content() -> None:
    views = _views("[来源: sources/a.md]\n<!-- pagebreak -->page two")

    warning = _attach_chunk_locators(views, doc_format="pdf", page_count=2)

    assert warning is None
    assert views[0][1]["locator"] == {"kind": "page", "start": 2, "end": 2}


def test_pdf_single_page_needs_no_marker() -> None:
    views = _views("first chunk", "second chunk")

    warning = _attach_chunk_locators(views, doc_format="pdf", page_count=1)

    assert warning is None
    assert all(
        metadata["locator"] == {"kind": "page", "start": 1, "end": 1}
        for _, metadata in views
    )


def test_pdf_mismatch_warns_and_removes_stale_page_locator() -> None:
    views = [("page one", {"locator": {"kind": "page", "start": 9, "end": 9}})]

    warning = _attach_chunk_locators(views, doc_format="pdf", page_count=2)

    assert "pagebreak count 0 != expected 1" in warning
    assert "locator" not in views[0][1]


def test_structural_locators_are_additive_and_one_based() -> None:
    slide = [("part a", {"slide_index": 8}), ("part b", {"slide_index": 8})]
    sheet = [("rows", {"sheet_name": "不具合管理一覧"})]
    timed = [("speech", {
        "start_time": "00:00",
        "end_time": "02:42",
        "start_seconds": 0,
        "end_seconds": 162,
    })]

    assert _attach_chunk_locators(slide, doc_format="pptx") is None
    assert _attach_chunk_locators(sheet, doc_format="xlsx") is None
    assert _attach_chunk_locators(timed, doc_format="wav") is None

    assert [m["locator"] for _, m in slide] == [
        {"kind": "slide", "index": 9},
        {"kind": "slide", "index": 9},
    ]
    assert slide[0][1]["slide_index"] == 8
    assert sheet[0][1] == {
        "sheet_name": "不具合管理一覧",
        "locator": {"kind": "sheet", "name": "不具合管理一覧"},
    }
    assert timed[0][1]["start_time"] == "00:00"
    assert timed[0][1]["locator"] == {
        "kind": "time",
        "start_seconds": 0,
        "end_seconds": 162,
    }


def test_locator_does_not_change_text_or_chunk_id() -> None:
    from docingest.chunkers.base import Chunk

    chunk = Chunk(
        text="slide content",
        metadata={"source": "sources/deck.md", "chunk_index": 3, "slide_index": 1},
    )
    text_before = chunk.text
    id_before = build_chunk_id(chunk)

    _attach_chunk_locators(
        [(chunk.text, chunk.metadata)], doc_format="pptx"
    )

    assert chunk.text == text_before
    assert build_chunk_id(chunk) == id_before


def test_cached_records_gain_locator_without_reprocessing(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    output = tmp_path / "knowledge"
    source.write_text("# Source\n\nbody\n", encoding="utf-8")
    overrides = {
        "parsing": {"vision": {"enabled": False}},
        "safety": {"enabled": False},
        "knowledge_map": {"enabled": False},
        "quality_report": {"enabled": False},
        "run_log": {"enabled": False},
    }
    ingest(source, output=output, purpose="rag", config_overrides=overrides)

    meta_path = next((output / ".cache").glob("*.meta.json"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["format"] = "pdf"
    meta["index_entry"]["pages"] = 3
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    chunks_path = output / "chunks.jsonl"
    records = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines()]
    records[0]["text"] = f"one{PAGEBREAK_MARKER}two{PAGEBREAK_MARKER}three"
    records[0]["metadata"].pop("locator", None)
    chunks_path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )

    events: list[dict] = []
    ingest(
        source,
        output=output,
        purpose="rag",
        config_overrides=overrides,
        on_progress=events.append,
    )
    replayed = json.loads(chunks_path.read_text(encoding="utf-8").splitlines()[0])
    upgraded_bytes = chunks_path.read_bytes()

    third_events: list[dict] = []
    ingest(
        source,
        output=output,
        purpose="rag",
        config_overrides=overrides,
        on_progress=third_events.append,
    )

    assert events[-1]["status"] == "cached"
    assert replayed["metadata"]["locator"] == {
        "kind": "page",
        "start": 1,
        "end": 3,
    }
    assert third_events[-1]["status"] == "cached"
    assert chunks_path.read_bytes() == upgraded_bytes
