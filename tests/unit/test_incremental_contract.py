"""Regression tests for incremental-output consistency."""

import json
from pathlib import Path

from docingest.chunkers import create_chunker
from docingest.config import load_config
from docingest.incremental import build_meta, is_cache_valid
from docingest.parsers import create_parser
from docingest.pipeline import run_pipeline


def _offline_config(output_dir: Path, *, incremental: bool = False) -> dict:
    return load_config(cli_overrides={
        "output": {"dir": str(output_dir)},
        "parsing": {"vision": {"enabled": False}},
        "knowledge_map": {"enabled": False},
        "quality_report": {"enabled": False},
        "run_log": {"enabled": False},
        "safety": {"enabled": False},
        "incremental": {"enabled": incremental},
    })


def test_zero_chunk_rebuild_truncates_previous_chunks(tmp_path: Path) -> None:
    """A successful rebuild with zero chunks must not leave old records."""
    source = tmp_path / "source.md"
    output = tmp_path / "knowledge"
    config = _offline_config(output)

    source.write_text("# Title\n\nFirst build has content.\n", encoding="utf-8")
    first = run_pipeline(
        [source], config, create_parser(config), create_chunker(config)
    )
    chunks_path = output / "chunks.jsonl"
    assert first.total_chunks == 1
    assert chunks_path.read_text(encoding="utf-8").strip()

    source.write_text("", encoding="utf-8")
    second = run_pipeline(
        [source], config, create_parser(config), create_chunker(config)
    )

    assert second.successful == 1
    assert second.total_chunks == 0
    assert chunks_path.exists()
    assert chunks_path.read_text(encoding="utf-8") == ""


def test_cache_contract_rejects_version_one_metadata(tmp_path: Path) -> None:
    """Pre-contract-version metadata must invalidate after the upgrade."""
    source = tmp_path / "source.md"
    source.write_text("content", encoding="utf-8")
    output = tmp_path / "knowledge"
    source_md = output / "sources" / "source.md"
    source_md.parent.mkdir(parents=True)
    source_md.write_text("content", encoding="utf-8")

    meta = build_meta(
        file_path=source,
        cache_key="key",
        config_hash="config",
        format_str="md",
        source_md_rel="sources/source.md",
        asset_rels=[],
        chunk_ids=[],
        index_entry={},
    )
    assert meta["version"] > 1

    old_meta = dict(meta)
    old_meta["version"] = 1
    valid, reason = is_cache_valid(old_meta, "config", output, {})
    assert valid is False
    assert "version mismatch" in reason


def test_same_stem_different_extensions_keep_unique_ids_across_cache(
    tmp_path: Path,
) -> None:
    """same.md + same.txt must not overwrite each other on cache reuse."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    markdown = input_dir / "same.md"
    text = input_dir / "same.txt"
    markdown.write_text("# FROM_MD\n\nalpha\n", encoding="utf-8")
    text.write_text("FROM_TXT beta\n", encoding="utf-8")

    output = tmp_path / "knowledge"
    config = _offline_config(output, incremental=True)
    first = run_pipeline(
        [markdown, text], config, create_parser(config), create_chunker(config)
    )
    chunks_path = output / "chunks.jsonl"
    first_bytes = chunks_path.read_bytes()
    first_records = [
        json.loads(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
    ]

    assert first.successful == 2
    assert len(first_records) == 2
    assert len({record["id"] for record in first_records}) == 2
    assert any("FROM\\_MD" in record["text"] for record in first_records)
    assert any("FROM\\_TXT" in record["text"] for record in first_records)

    second = run_pipeline(
        [markdown, text], config, create_parser(config), create_chunker(config)
    )
    second_records = [
        json.loads(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
    ]

    assert sum(file.status == "cached" for file in second.files) == 2
    assert chunks_path.read_bytes() == first_bytes
    assert [record["text"] for record in second_records] == [
        record["text"] for record in first_records
    ]
