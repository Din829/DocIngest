# -*- coding: utf-8 -*-
"""URL provenance — .origin.json sidecar and the `resource` field.

Covers: sidecar write/lookup round-trip, idempotency, graceful None for
pre-sidecar caches and ordinary local files, cache-hit collection excluding
the sidecar, and an offline end-to-end run proving `resource` reaches
frontmatter + index.json + chunk lineage while staying OUT of flat chunk
metadata (blacklisted — chunks read it from lineage.original_input.url).

Run:
    python -m pytest tests/unit/test_url_origin.py -q
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.utils.url_resolver import (
    _ORIGIN_FILENAME,
    _write_origin,
    lookup_url_origin,
    resolve_url,
)

URL = "https://www.example.com/video/abc123/"


def _config_for(output_dir: Path) -> dict:
    return {"output": {"dir": str(output_dir)}}


def _media_dir(output_dir: Path, name: str = "aaaa0000bbbb") -> Path:
    d = output_dir / ".cache" / "_media" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_write_and_lookup_round_trip():
    out = Path(tempfile.mkdtemp(prefix="docingest_origin_"))
    d = _media_dir(out)
    _write_origin(d, URL)
    media_file = d / "abc123.mp3"
    media_file.write_bytes(b"fake audio")
    assert lookup_url_origin(media_file, _config_for(out)) == URL


def test_write_origin_idempotent():
    out = Path(tempfile.mkdtemp(prefix="docingest_origin_"))
    d = _media_dir(out)
    _write_origin(d, URL)
    # Second write with a different URL must NOT overwrite the first record.
    _write_origin(d, "https://other.example.com/")
    data = json.loads((d / _ORIGIN_FILENAME).read_text(encoding="utf-8"))
    assert data["url"] == URL


def test_lookup_pre_sidecar_cache_returns_none():
    # Media-cache file downloaded before origin manifests existed.
    out = Path(tempfile.mkdtemp(prefix="docingest_origin_"))
    d = _media_dir(out)
    media_file = d / "old.mp3"
    media_file.write_bytes(b"x")
    assert lookup_url_origin(media_file, _config_for(out)) is None


def test_lookup_ordinary_local_file_returns_none():
    out = Path(tempfile.mkdtemp(prefix="docingest_origin_"))
    local = out / "report.pdf"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"x")
    assert lookup_url_origin(local, _config_for(out)) is None


def test_resolve_url_cache_hit_excludes_sidecar():
    """Cache hit must return media files only — never the sidecar itself —
    and must backfill the sidecar for pre-sidecar caches."""
    out = Path(tempfile.mkdtemp(prefix="docingest_origin_"))
    config = _config_for(out)
    # Place a cached media file at the hash dir resolve_url will compute.
    import hashlib
    url_hash = hashlib.md5(URL.encode("utf-8")).hexdigest()[:12]
    d = _media_dir(out, url_hash)
    (d / "abc123.mp3").write_bytes(b"fake audio")

    files = resolve_url(URL, config)
    assert files is not None
    assert [f.name for f in files] == ["abc123.mp3"]
    # Backfill: the pre-sidecar cache now carries provenance.
    assert lookup_url_origin(d / "abc123.mp3", config) == URL

    # Second hit: sidecar exists and is still excluded from the file list.
    files = resolve_url(URL, config)
    assert files is not None
    assert [f.name for f in files] == ["abc123.mp3"]


def test_url_provenance_end_to_end():
    """Offline e2e: a file living in the media cache with an origin sidecar
    flows `resource` into frontmatter + index.json + lineage, and keeps it
    out of flat chunk metadata."""
    import docingest

    out = Path(tempfile.mkdtemp(prefix="docingest_origin_e2e_"))
    d = _media_dir(out)
    doc = d / "transcript.md"
    doc.write_text(
        "# Talk\n\nSpoken content of the talk goes here.\n\n"
        "## Part two\n\nMore transcript body.\n",
        encoding="utf-8",
    )
    _write_origin(d, URL)

    result = docingest.ingest(
        doc,
        output=out,
        outputs=["markdown", "chunks", "index"],
        config_overrides={
            "knowledge_map": {"enrich_with_ai": False},
            "run_log": {"enabled": False},
        },
    )
    assert result.stats["successful"] == 1, result.stats

    # Frontmatter carries `resource`.
    md = result.markdown_files[0]
    assert md["metadata"].get("resource") == URL, md["metadata"]

    # index.json carries `resource` per file.
    entry = result.index["files"][0]
    assert entry.get("resource") == URL, entry

    # Chunks: flat metadata clean, lineage carries the URL.
    assert len(result.chunks) > 0
    for chunk in result.chunks:
        meta = chunk["metadata"]
        assert "resource" not in meta, "resource must be blacklisted from flat chunk metadata"
        assert meta["lineage"]["original_input"].get("url") == URL, meta["lineage"]
