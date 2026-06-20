# -*- coding: utf-8 -*-
"""Unit tests for related_enrichment — Jaccard-based cross-file related links.

Covers: Jaccard math, threshold gating, max_links cap, big-file bias
avoidance (why Jaccard not raw count), enable/disable, idempotency, and the
OKF-compatible link format.
"""

import tempfile
from pathlib import Path

import yaml

from docingest.output.related_enrichment import (
    _jaccard,
    _link_for,
    enrich_sources_with_related,
)


def test_jaccard_basic():
    assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0
    assert abs(_jaccard({"a", "b"}, {"a", "c"}) - 1 / 3) < 1e-9


def test_jaccard_empty():
    assert _jaccard(set(), {"a"}) == 0.0
    assert _jaccard({"a"}, set()) == 0.0


def test_jaccard_avoids_bigfile_bias():
    # A keyword-rich file shares 2 words with a small file, but its set is huge
    # → Jaccard stays low (the whole reason we use it over raw shared-count).
    big = {f"k{i}" for i in range(50)} | {"x", "y"}
    small = {"x", "y", "z"}
    assert len(big & small) == 2          # raw count would call them related
    assert _jaccard(big, small) < 0.05    # Jaccard says barely related


def test_link_for_okf_format():
    assert _link_for({"path": "sources/orders.md", "title": "Orders"}) == "[Orders](/sources/orders.md)"


def test_link_for_fallback_title():
    assert _link_for({"path": "sources/foo.md"}) == "[foo](/sources/foo.md)"


def test_link_for_no_path():
    assert _link_for({"title": "x"}) is None


def _make_file(d: Path, name: str) -> None:
    (d / "sources").mkdir(parents=True, exist_ok=True)
    (d / "sources" / name).write_text(
        f"---\ntitle: {Path(name).stem}\nformat: pdf\n---\n\nbody of {name}\n",
        encoding="utf-8",
    )


def _read_related(d: Path, name: str):
    txt = (d / "sources" / name).read_text(encoding="utf-8")
    fm = txt[3:txt.find("\n---", 3)]
    return yaml.safe_load(fm).get("related")


def _cfg(**over):
    base = {"enabled": True, "max_links": 3, "min_similarity": 0.15}
    base.update(over)
    return {"output": {"derived_metadata": {"related": base}}}


def test_related_written_for_similar_files():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for n in ("a.md", "b.md", "c.md"):
            _make_file(d, n)
        km = {"files": [
            {"path": "sources/a.md", "title": "A", "keywords": ["x", "y", "z"]},
            {"path": "sources/b.md", "title": "B", "keywords": ["x", "y", "z"]},
            {"path": "sources/c.md", "title": "C", "keywords": ["p", "q", "r"]},
        ]}
        n = enrich_sources_with_related(km, d, _cfg())
        assert n == 2  # a, b related to each other; c unrelated
        assert _read_related(d, "a.md") == ["[B](/sources/b.md)"]
        assert _read_related(d, "c.md") is None


def test_threshold_gates_weak_links():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _make_file(d, "a.md")
        _make_file(d, "b.md")
        km = {"files": [
            {"path": "sources/a.md", "title": "A", "keywords": ["x", "y", "z"]},
            {"path": "sources/b.md", "title": "B", "keywords": ["x", "p", "q"]},
        ]}  # share 1 of 5 union → Jaccard 0.2
        n = enrich_sources_with_related(km, d, _cfg(min_similarity=0.5))
        assert n == 0  # 0.2 < 0.5 → nothing


def test_max_links_cap():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for n in ("a.md", "b.md", "c.md", "e.md"):
            _make_file(d, n)
        km = {"files": [
            {"path": f"sources/{x}.md", "title": x.upper(), "keywords": ["x", "y", "z"]}
            for x in ("a", "b", "c", "e")
        ]}
        enrich_sources_with_related(km, d, _cfg(max_links=2))
        assert len(_read_related(d, "a.md")) == 2  # capped though 3 qualify


def test_disabled_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _make_file(d, "a.md")
        _make_file(d, "b.md")
        km = {"files": [
            {"path": "sources/a.md", "title": "A", "keywords": ["x", "y"]},
            {"path": "sources/b.md", "title": "B", "keywords": ["x", "y"]},
        ]}
        assert enrich_sources_with_related(km, d, _cfg(enabled=False)) == 0
        assert _read_related(d, "a.md") is None


def test_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _make_file(d, "a.md")
        _make_file(d, "b.md")
        km = {"files": [
            {"path": "sources/a.md", "title": "A", "keywords": ["x", "y", "z"]},
            {"path": "sources/b.md", "title": "B", "keywords": ["x", "y", "z"]},
        ]}
        first = enrich_sources_with_related(km, d, _cfg())
        second = enrich_sources_with_related(km, d, _cfg())
        assert first == 2
        assert second == 0  # re-run changes nothing → idempotent
