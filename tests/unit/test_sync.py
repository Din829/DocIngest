"""Explicit directory-sync regression and deletion-safety tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import docingest.pipeline as pipeline_mod
from docingest.api import ingest
from docingest.chunkers import create_chunker
from docingest.config import load_config
from docingest.incremental import finalize_sync
from docingest.parsers import create_parser
from docingest.pipeline import FileResult, run_pipeline


_OFFLINE = {
    "parsing": {"vision": {"enabled": False}},
    "safety": {"enabled": False},
    "knowledge_map": {"enabled": False},
    "quality_report": {"enabled": False},
    "run_log": {"enabled": False},
    "hooks": {"enabled": False},
}


def _write(path: Path, marker: str) -> None:
    path.write_text(f"# {path.stem}\n\n{marker}\n", encoding="utf-8")


def _sources(output: Path) -> set[str]:
    return {p.name for p in (output / "sources").glob("*.md")}


def _index_originals(output: Path) -> set[str]:
    data = json.loads((output / "index.json").read_text(encoding="utf-8"))
    return {entry["original_file"] for entry in data["files"]}


def test_sync_bootstraps_existing_library_and_prunes_deleted_input(
    tmp_path: Path,
) -> None:
    root = tmp_path / "input"
    output = tmp_path / "knowledge"
    root.mkdir()
    _write(root / "alpha.md", "ALPHA")
    _write(root / "beta.md", "BETA")

    # Existing knowledge bases have cache metadata but no sync manifest.
    ingest(root, output=output, purpose="rag", config_overrides=_OFFLINE)
    (root / "beta.md").unlink()
    result = ingest(
        root, output=output, purpose="rag", config_overrides=_OFFLINE, sync=True
    )

    assert _sources(output) == {"alpha.md"}
    assert _index_originals(output) == {"alpha.md"}
    assert result.stats["sync"]["removed_files"] == 1
    assert result.stats["sync"]["removed_cache_entries"] == 1
    assert (output / ".cache" / "sync-manifest.json").exists()


def test_sync_rename_removes_old_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "input"
    output = tmp_path / "knowledge"
    root.mkdir()
    _write(root / "beta.md", "BETA")
    ingest(
        root, output=output, purpose="rag", config_overrides=_OFFLINE, sync=True
    )

    (root / "beta.md").rename(root / "gamma.md")
    result = ingest(
        root, output=output, purpose="rag", config_overrides=_OFFLINE, sync=True
    )

    assert _sources(output) == {"gamma.md"}
    assert _index_originals(output) == {"gamma.md"}
    assert result.stats["sync"]["removed_files"] == 1


def test_ordinary_partial_ingest_never_prunes_sync_library(tmp_path: Path) -> None:
    root = tmp_path / "input"
    output = tmp_path / "knowledge"
    root.mkdir()
    _write(root / "alpha.md", "ALPHA")
    _write(root / "beta.md", "BETA")
    ingest(
        root, output=output, purpose="rag", config_overrides=_OFFLINE, sync=True
    )

    result = ingest(
        root / "alpha.md",
        output=output,
        purpose="rag",
        config_overrides=_OFFLINE,
    )

    assert result.stats["sync"] == {}
    assert _sources(output) == {"alpha.md", "beta.md"}


def test_sync_empty_directory_clears_owned_library(tmp_path: Path) -> None:
    root = tmp_path / "input"
    output = tmp_path / "knowledge"
    root.mkdir()
    _write(root / "alpha.md", "ALPHA")
    ingest(
        root, output=output, purpose="rag", config_overrides=_OFFLINE, sync=True
    )

    (root / "alpha.md").unlink()
    result = ingest(
        root, output=output, purpose="rag", config_overrides=_OFFLINE, sync=True
    )

    assert result.stats["sync"]["removed_files"] == 1
    assert _sources(output) == set()
    assert _index_originals(output) == set()
    assert (output / "chunks.jsonl").read_text(encoding="utf-8") == ""


def test_sync_respects_configured_source_directory(tmp_path: Path) -> None:
    root = tmp_path / "input"
    output = tmp_path / "knowledge"
    root.mkdir()
    _write(root / "alpha.md", "ALPHA")
    overrides = {**_OFFLINE, "output": {"sources_dir": "generated/markdown"}}
    ingest(root, output=output, config_overrides=overrides, sync=True)

    (root / "alpha.md").unlink()
    result = ingest(root, output=output, config_overrides=overrides, sync=True)

    assert result.stats["sync"]["removed_files"] == 1
    assert not (output / "generated" / "markdown" / "alpha.md").exists()


def test_failed_sync_preserves_previous_manifest_and_removed_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "input"
    output = tmp_path / "knowledge"
    root.mkdir()
    _write(root / "alpha.md", "ALPHA")
    _write(root / "beta.md", "BETA")
    ingest(
        root, output=output, purpose="rag", config_overrides=_OFFLINE, sync=True
    )
    manifest = output / ".cache" / "sync-manifest.json"
    before = manifest.read_bytes()
    (root / "beta.md").unlink()

    def fail_file(file_path, parser, chunker, config, output_dir,
                  existing_names=None, on_file_progress=None):
        return FileResult(
            original_file=str(file_path),
            success=False,
            error="simulated parse failure",
            error_type="parse_error",
        ), []

    monkeypatch.setattr(pipeline_mod, "process_single_file", fail_file)
    config = load_config(cli_overrides={
        **_OFFLINE,
        "output": {"dir": str(output)},
        "incremental": {"enabled": True, "force": True},
    })
    result = run_pipeline(
        [root],
        config,
        create_parser(config),
        create_chunker(config),
        sync_root=root,
    )

    assert result.failed == 1
    assert result.sync["skipped"] is True
    assert (output / "sources" / "beta.md").exists()
    assert manifest.read_bytes() == before


def test_safety_aborted_sync_preserves_previous_manifest_and_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "input"
    output = tmp_path / "knowledge"
    root.mkdir()
    _write(root / "alpha.md", "ALPHA")
    _write(root / "beta.md", "BETA")
    ingest(
        root, output=output, purpose="rag", config_overrides=_OFFLINE, sync=True
    )
    manifest = output / ".cache" / "sync-manifest.json"
    before = manifest.read_bytes()
    (root / "beta.md").unlink()
    strict = {
        **_OFFLINE,
        "safety": {
            "enabled": True,
            "mode": "strict",
            "per_run": {"max_total_files": 0},
        },
    }

    result = ingest(
        root, output=output, purpose="rag", config_overrides=strict, sync=True
    )

    assert result.stats["safety"]["aborted"] is True
    assert result.stats["sync"] == {}
    assert (output / "sources" / "beta.md").exists()
    assert manifest.read_bytes() == before


def test_sync_rejects_file_multiple_inputs_and_different_bound_root(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    output = tmp_path / "knowledge"
    root_a.mkdir()
    root_b.mkdir()
    _write(root_a / "one.md", "ONE")
    _write(root_b / "two.md", "TWO")

    with pytest.raises(ValueError, match="existing local directory"):
        ingest(root_a / "one.md", output=output, sync=True)
    with pytest.raises(ValueError, match="exactly one local directory"):
        ingest([root_a, root_b], output=output, sync=True)

    ingest(root_a, output=output, config_overrides=_OFFLINE, sync=True)
    with pytest.raises(ValueError, match="different sync root"):
        ingest(root_b, output=output, config_overrides=_OFFLINE, sync=True)
    assert (output / "sources" / "one.md").exists()


def test_finalize_sync_rejects_path_traversal(tmp_path: Path) -> None:
    output = tmp_path / "knowledge"
    cache = output / ".cache"
    root = tmp_path / "input"
    output.mkdir()
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive", encoding="utf-8")

    previous = {
        "removed": {
            "cache_key": "bad",
            "source_md": "../outside.txt",
            "assets": [],
        }
    }
    with pytest.raises(ValueError, match="sync may only remove"):
        finalize_sync(output, cache, root, previous, {})
    assert outside.read_text(encoding="utf-8") == "must survive"


def test_finalize_sync_preserves_artifact_still_owned_by_live_input(
    tmp_path: Path,
) -> None:
    output = tmp_path / "knowledge"
    cache = output / ".cache"
    root = tmp_path / "input"
    source = output / "sources" / "shared.md"
    source.parent.mkdir(parents=True)
    root.mkdir()
    source.write_text("shared", encoding="utf-8")
    entry = {"cache_key": "old", "source_md": "sources/shared.md", "assets": []}

    summary = finalize_sync(
        output,
        cache,
        root,
        {"removed": entry},
        {"live": {**entry, "cache_key": "new"}},
    )

    assert source.exists()
    assert summary["removed_files"] == 1
    assert summary["removed_artifacts"] == []


def test_finalize_sync_removes_owned_source_asset_and_cache(tmp_path: Path) -> None:
    output = tmp_path / "knowledge"
    cache = output / ".cache"
    root = tmp_path / "input"
    source = output / "sources" / "gone.md"
    asset = output / "assets" / "gone-image.png"
    source.parent.mkdir(parents=True)
    asset.parent.mkdir(parents=True)
    cache.mkdir(parents=True)
    root.mkdir()
    source.write_text("gone", encoding="utf-8")
    asset.write_bytes(b"png")
    (cache / "gone-key.meta.json").write_text("{}", encoding="utf-8")
    previous = {
        "gone": {
            "cache_key": "gone-key",
            "source_md": "sources/gone.md",
            "assets": ["assets/gone-image.png"],
        }
    }

    summary = finalize_sync(output, cache, root, previous, {})

    assert not source.exists()
    assert not asset.exists()
    assert not (cache / "gone-key.meta.json").exists()
    assert summary["removed_artifacts"] == [
        "sources/gone.md",
        "assets/gone-image.png",
    ]
    assert summary["removed_cache_entries"] == 1
