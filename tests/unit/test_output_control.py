"""
Output control — purpose presets + outputs whitelist + post-run cleanup.

Verifies the "produce exactly the artefacts the caller asked for" feature:
  1. purpose → outputs resolution (precedence, unknown handling)
  2. outputs whitelist → config translation (generate-off vs cleanup-token)
  3. _finalize_artifacts actually deletes index/assets/errors when told,
     keeps .cache, and clears meta.outputs.assets for incremental safety
  4. backward compatibility: no purpose / no outputs → produce everything

Pure logic — no LLM calls, no real ingest. Run:  python tests/unit/test_output_control.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from docingest.api import (
    _resolve_outputs,
    _apply_output_whitelist,
    _PURPOSE_PRESETS,
    _ALL_OUTPUTS,
)
from docingest.config import get_nested
from docingest.pipeline import _finalize_artifacts


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# ---------------------------------------------------------------------------
# 1. purpose → outputs resolution
# ---------------------------------------------------------------------------

def test_purpose_resolution():
    print("test_purpose_resolution")

    # None / "full" → produce everything (legacy)
    _check(_resolve_outputs(None, None) is None, "no purpose, no outputs → None (all)")
    _check(_resolve_outputs(None, "full") is None, "purpose=full → None (all)")

    # presets expand to their list
    _check(_resolve_outputs(None, "markdown") == ["markdown"], "markdown preset")
    _check(_resolve_outputs(None, "rag") == ["markdown", "chunks", "index"], "rag preset")
    _check(
        _resolve_outputs(None, "agentic") == ["markdown", "index", "knowledge_map"],
        "agentic preset",
    )

    # explicit outputs wins over purpose
    _check(
        _resolve_outputs(["chunks"], "markdown") == ["chunks"],
        "explicit outputs wins over purpose",
    )

    # rag preset includes chunks → chunking will stay enabled (auto-cut)
    _check("chunks" in _PURPOSE_PRESETS["rag"], "rag preset carries chunks (auto-cut on)")

    # unknown purpose fails fast
    try:
        _resolve_outputs(None, "nonsense")
        raise AssertionError("unknown purpose should raise ValueError")
    except ValueError:
        print("  ok: unknown purpose raises ValueError")

    # returned list is a COPY (mutating it must not corrupt the preset)
    got = _resolve_outputs(None, "rag")
    assert got is not None
    got.append("XXX")
    _check("XXX" not in _PURPOSE_PRESETS["rag"], "preset list not mutated by caller")


# ---------------------------------------------------------------------------
# 2. outputs whitelist → config translation
# ---------------------------------------------------------------------------

def test_whitelist_translation():
    print("test_whitelist_translation")

    # markdown-only: chunks/map/quality/run_log generated-off; index/assets/
    # errors go to the cleanup set.
    layered: dict = {}
    _apply_output_whitelist(layered, ["markdown"])

    _check(get_nested(layered, "chunking.enabled") is False, "markdown → chunking off")
    _check(get_nested(layered, "knowledge_map.enabled") is False, "markdown → knowledge_map off")
    _check(get_nested(layered, "quality_report.enabled") is False, "markdown → quality_report off")
    _check(get_nested(layered, "run_log.enabled") is False, "markdown → run_log off")

    cleanup = set(get_nested(layered, "output._cleanup") or [])
    _check(cleanup == {"index", "assets", "errors"}, f"markdown → cleanup={{index,assets,errors}} (got {cleanup})")

    # rag: keeps chunks + index → chunking stays ON, index NOT cleaned,
    # errors NOT cleaned (index kept). assets cleaned (not requested).
    layered2: dict = {}
    _apply_output_whitelist(layered2, ["markdown", "chunks", "index"])
    _check(get_nested(layered2, "chunking.enabled") is None, "rag → chunking untouched (stays default-on)")
    cleanup2 = set(get_nested(layered2, "output._cleanup") or [])
    _check("index" not in cleanup2, "rag → index kept (not in cleanup)")
    _check("errors" not in cleanup2, "rag → errors kept (index present)")
    _check("assets" in cleanup2, "rag → assets cleaned (not requested)")

    # unknown output fails fast
    try:
        _apply_output_whitelist({}, ["markdown", "bogus"])
        raise AssertionError("unknown output should raise")
    except ValueError:
        print("  ok: unknown output raises ValueError")

    # assets is a valid whitelist member now
    _check("assets" in _ALL_OUTPUTS, "assets is in _ALL_OUTPUTS")


def test_dependency_auto_expansion():
    """knowledge_map reads chunks + index at build time. Asking for the map
    without its deps must NOT disable chunking; the deps are produced then
    cleaned up (runtime-need ≠ keep-on-disk)."""
    print("test_dependency_auto_expansion")

    # outputs=["knowledge_map"] — deps forced on, then cleaned
    layered: dict = {}
    _apply_output_whitelist(layered, ["knowledge_map"])
    _check(
        get_nested(layered, "chunking.enabled") is not False,
        "map → chunking NOT disabled (chunks is a build dep)",
    )
    _check(get_nested(layered, "knowledge_map.enabled") is not False, "map → map stays on")
    cleanup = set(get_nested(layered, "output._cleanup") or [])
    _check("chunks" in cleanup, "map → chunks built-then-deleted (user didn't keep it)")
    _check("index" in cleanup, "map → index built-then-deleted (user didn't keep it)")

    # agentic preset: keeps index (user wants it), chunks built-then-deleted
    layered2: dict = {}
    _apply_output_whitelist(layered2, ["markdown", "index", "knowledge_map"])
    _check(get_nested(layered2, "chunking.enabled") is not False, "agentic → chunking on (dep)")
    cleanup2 = set(get_nested(layered2, "output._cleanup") or [])
    _check("chunks" in cleanup2, "agentic → chunks cleaned (not requested)")
    _check("index" not in cleanup2, "agentic → index kept (requested)")

    # CONTRAST: markdown doesn't want the map → chunking stays OFF (no
    # over-generation). This proves deps are forced ONLY when truly needed.
    layered3: dict = {}
    _apply_output_whitelist(layered3, ["markdown"])
    _check(
        get_nested(layered3, "chunking.enabled") is False,
        "markdown (no map) → chunking OFF (deps not over-forced)",
    )


# ---------------------------------------------------------------------------
# 3. _finalize_artifacts — real files in a temp dir
# ---------------------------------------------------------------------------

def _seed_kb(root: Path) -> None:
    """Build a fake-but-realistic knowledge base on disk."""
    (root / "sources").mkdir(parents=True)
    (root / "sources" / "doc.md").write_text("# hi", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "doc-page-001.png").write_bytes(b"\x89PNG")
    (root / "index.json").write_text('{"files": []}', encoding="utf-8")
    (root / "errors.json").write_text("[]", encoding="utf-8")
    (root / "chunks.jsonl").write_text("", encoding="utf-8")
    # incremental meta referencing the asset
    cache = root / ".cache"
    cache.mkdir()
    meta = {
        "version": 1,
        "cache_key": "k_1",
        "outputs": {
            "source_md": "sources/doc.md",
            "assets": ["assets/doc-page-001.png"],
            "chunk_ids": [],
        },
        "index_entry": {"file": "doc.md"},
    }
    (cache / "k_1.meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_finalize_markdown_only():
    print("test_finalize_markdown_only")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_kb(root)

        # Config mirrors what api would build for purpose=markdown
        config = {
            "output": {"assets_dir": "assets", "index_file": "index.json",
                       "_cleanup": ["index", "assets", "errors"]},
            "incremental": {"cache_dir": ".cache"},
            "error_handling": {"report_file": "errors.json"},
        }
        _finalize_artifacts(root, config)

        # sources kept; index/assets/errors gone; .cache kept
        _check((root / "sources" / "doc.md").exists(), "sources/doc.md kept")
        _check(not (root / "index.json").exists(), "index.json deleted")
        _check(not (root / "assets").exists(), "assets/ deleted")
        _check(not (root / "errors.json").exists(), "errors.json deleted")
        _check((root / ".cache").exists(), ".cache/ kept (incremental intact)")

        # meta.outputs.assets cleared → next is_cache_valid asset check passes
        meta = json.loads((root / ".cache" / "k_1.meta.json").read_text(encoding="utf-8"))
        _check(meta["outputs"]["assets"] == [], "meta.outputs.assets cleared")
        _check(meta["index_entry"] == {"file": "doc.md"}, "meta.index_entry preserved (cache hit still works)")
        _check(meta["outputs"]["source_md"] == "sources/doc.md", "meta.source_md preserved")


def test_finalize_noop_when_no_cleanup():
    print("test_finalize_noop_when_no_cleanup")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_kb(root)
        # No output._cleanup → legacy full-output → nothing deleted
        config = {
            "output": {"assets_dir": "assets", "index_file": "index.json"},
            "incremental": {"cache_dir": ".cache"},
        }
        _finalize_artifacts(root, config)
        _check((root / "index.json").exists(), "no cleanup set → index.json kept")
        _check((root / "assets").exists(), "no cleanup set → assets kept")
        _check((root / "errors.json").exists(), "no cleanup set → errors kept")


def test_finalize_rag_keeps_assets_and_index():
    print("test_finalize_rag_keeps_assets_and_index")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _seed_kb(root)
        # purpose=rag keeps index (+chunks); assets not requested → cleaned.
        # No dangling ref risk: index.json here has empty files list.
        config = {
            "output": {"assets_dir": "assets", "index_file": "index.json",
                       "_cleanup": ["assets"]},
            "incremental": {"cache_dir": ".cache"},
            "error_handling": {"report_file": "errors.json"},
        }
        _finalize_artifacts(root, config)
        _check((root / "index.json").exists(), "rag → index.json kept")
        _check((root / "errors.json").exists(), "rag → errors.json kept")
        _check(not (root / "assets").exists(), "rag → assets deleted (not requested)")
        meta = json.loads((root / ".cache" / "k_1.meta.json").read_text(encoding="utf-8"))
        _check(meta["outputs"]["assets"] == [], "rag → meta.assets cleared too")


# ---------------------------------------------------------------------------
# 4. End-to-end through the real pipeline (Vision OFF — no LLM cost).
#    Plain-text Markdown never triggers Vision, so this exercises the FULL
#    ingest → write → finalize path without spending a cent.
# ---------------------------------------------------------------------------

_FIXTURE_DOCS = Path(__file__).resolve().parents[1] / "incremental" / "docs"
# config_overrides shared by the e2e runs: Vision off (no API), safety off
# (these tiny files trip nothing, but be explicit so the test is hermetic).
_E2E_OVERRIDES = {
    "parsing.vision.enabled": False,
    "safety.mode": "off",
}


def _listing(root: Path) -> set[str]:
    """Top-level entries in the output dir (names only), excluding meta.json
    the library facade writes. .cache is reported as a single entry."""
    out = set()
    for p in root.iterdir():
        out.add(p.name)
    return out


def test_e2e_markdown_only():
    print("test_e2e_markdown_only (real pipeline, vision off)")
    import docingest

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "kb"
        result = docingest.ingest(
            _FIXTURE_DOCS / "alpha.md",
            output=out,
            purpose="markdown",
            config_overrides=_E2E_OVERRIDES,
        )
        _check(result.stats.get("successful") == 1, "1 file processed")

        names = _listing(out)
        # The product:
        _check("sources" in names, "sources/ present")
        _check((out / "sources" / "alpha.md").exists(), "sources/alpha.md written")
        # Cleaned up:
        _check("index.json" not in names, "index.json gone (markdown purpose)")
        _check("chunks.jsonl" not in names, "chunks.jsonl never produced")
        _check("knowledge_map.yaml" not in names, "knowledge_map gone")
        _check("quality_report.json" not in names, "quality_report gone")
        _check("assets" not in names, "assets/ gone")
        # .cache survives so incremental still works
        _check(".cache" in names, ".cache/ kept")

        # markdown content really is the product
        _check(len(result.markdown_files) == 1, "markdown read back into result")


def test_e2e_rag_produces_chunks():
    print("test_e2e_rag_produces_chunks (real pipeline, vision off)")
    import docingest

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "kb"
        result = docingest.ingest(
            _FIXTURE_DOCS / "alpha.md",
            output=out,
            purpose="rag",
            config_overrides=_E2E_OVERRIDES,
        )
        names = _listing(out)
        _check("sources" in names, "rag → sources/ present")
        _check("chunks.jsonl" in names, "rag → chunks.jsonl produced (auto-cut on)")
        _check("index.json" in names, "rag → index.json kept")
        _check(len(result.chunks) > 0, "rag → chunks read back, chunking really happened")
        # map / quality not requested by rag preset
        _check("knowledge_map.yaml" not in names, "rag → knowledge_map not produced")


def test_e2e_full_is_backward_compatible():
    print("test_e2e_full_is_backward_compatible (no purpose/outputs = legacy)")
    import docingest

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "kb"
        docingest.ingest(
            _FIXTURE_DOCS / "alpha.md",
            output=out,
            config_overrides=_E2E_OVERRIDES,  # NO purpose, NO outputs
        )
        names = _listing(out)
        # Legacy behaviour: everything the config enables is produced + kept.
        _check("sources" in names, "full → sources/")
        _check("index.json" in names, "full → index.json kept (legacy)")
        _check("chunks.jsonl" in names, "full → chunks.jsonl kept (legacy)")
        _check("knowledge_map.yaml" in names, "full → knowledge_map kept (legacy)")


def test_e2e_incremental_repeat_markdown():
    """THE critical regression: running purpose=markdown TWICE on the same
    dir must hit the cache the 2nd time, NOT re-process. This is the坑 the
    meta.assets-clearing in _finalize_artifacts prevents — without it, the
    deleted assets would invalidate every cached file forever."""
    print("test_e2e_incremental_repeat_markdown (cache must hold across runs)")
    import docingest

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "kb"

        # Capture per-file status via on_progress so we can PROVE run 2 is a
        # cache hit, not a re-process. status ∈ {added, updated, cached, ...}.
        statuses_1: list[str] = []
        statuses_2: list[str] = []

        # Run 1 — first time → "added", writes meta + (then-cleaned) assets.
        docingest.ingest(
            _FIXTURE_DOCS / "alpha.md", output=out,
            purpose="markdown", config_overrides=_E2E_OVERRIDES,
            on_progress=lambda e: statuses_1.append(e.get("status", "")),
        )
        _check("added" in statuses_1, f"run1: file added (statuses={statuses_1})")
        _check("index.json" not in _listing(out), "run1: index cleaned")

        # Run 2 — same input + purpose. If meta.assets weren't cleared by
        # finalize, the deleted assets would invalidate the cache → "updated"
        # (re-process, re-burn cost). The fix makes it "cached".
        r2 = docingest.ingest(
            _FIXTURE_DOCS / "alpha.md", output=out,
            purpose="markdown", config_overrides=_E2E_OVERRIDES,
            on_progress=lambda e: statuses_2.append(e.get("status", "")),
        )
        _check(
            "cached" in statuses_2 and "updated" not in statuses_2,
            f"run2: CACHE HIT, not re-processed (statuses={statuses_2})",
        )
        _check(r2.stats.get("failed", 0) == 0, "run2: nothing failed")
        _check("index.json" not in _listing(out), "run2: index still cleaned")
        _check((out / "sources" / "alpha.md").exists(), "run2: source still present")


def test_e2e_agentic_with_map_dependency():
    """agentic preset really produces knowledge_map (which needs chunks built
    at runtime) while NOT leaving chunks.jsonl on disk. Proves the dependency
    auto-expansion works end-to-end, not just at the config layer."""
    print("test_e2e_agentic_with_map_dependency (real pipeline, vision+ai_summary off)")
    import docingest

    ov = dict(_E2E_OVERRIDES)
    ov["knowledge_map.ai_summary"] = False  # skip the 1 LLM summary call (save tokens)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "kb"
        docingest.ingest(
            _FIXTURE_DOCS / "alpha.md", output=out,
            purpose="agentic", config_overrides=ov,
        )
        names = _listing(out)
        _check("sources" in names, "agentic → sources/")
        _check("index.json" in names, "agentic → index.json kept (requested)")
        _check("knowledge_map.yaml" in names, "agentic → knowledge_map.yaml produced (dep satisfied)")
        # chunks was built (map needed it) but NOT kept
        _check("chunks.jsonl" not in names, "agentic → chunks.jsonl cleaned (built for map, not kept)")


def test_e2e_outputs_knowledge_map_only():
    """The exact undefined-corner the user flagged: outputs=['knowledge_map']
    with no explicit deps. Auto-expansion must make the map produce correctly
    AND clean up chunks+index after."""
    print("test_e2e_outputs_knowledge_map_only (dependency corner)")
    import docingest

    ov = dict(_E2E_OVERRIDES)
    ov["knowledge_map.ai_summary"] = False

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "kb"
        docingest.ingest(
            _FIXTURE_DOCS / "alpha.md", output=out,
            outputs=["knowledge_map"], config_overrides=ov,
        )
        names = _listing(out)
        _check("knowledge_map.yaml" in names, "map-only → knowledge_map.yaml produced (not partial/failed)")
        # map yaml is non-trivial (proves it built from real chunks, not empty)
        km = (out / "knowledge_map.yaml").read_text(encoding="utf-8")
        _check(len(km) > 50, f"map-only → knowledge_map.yaml non-empty ({len(km)} chars)")
        _check("chunks.jsonl" not in names, "map-only → chunks cleaned")
        _check("index.json" not in names, "map-only → index cleaned (not requested)")
        # markdown always survives
        _check("sources" in names, "map-only → sources/ still present")


def test_e2e_incremental_purpose_switch():
    """Switching purpose between runs (markdown → rag) must re-process to
    produce the newly-requested artefacts, not wrongly serve a stale cache."""
    print("test_e2e_incremental_purpose_switch (markdown → rag)")
    import docingest

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "kb"
        # Run 1: markdown only — no chunks on disk
        docingest.ingest(_FIXTURE_DOCS / "alpha.md", output=out,
                         purpose="markdown", config_overrides=_E2E_OVERRIDES)
        _check("chunks.jsonl" not in _listing(out), "after markdown: no chunks")

        # Run 2: rag — now we WANT chunks. Must appear (config_hash changed
        # because chunking.enabled flipped on → cache invalidates correctly).
        r2 = docingest.ingest(_FIXTURE_DOCS / "alpha.md", output=out,
                             purpose="rag", config_overrides=_E2E_OVERRIDES)
        _check("chunks.jsonl" in _listing(out), "after switch to rag: chunks.jsonl appears")
        _check("index.json" in _listing(out), "after switch to rag: index appears")
        _check(len(r2.chunks) > 0, "after switch to rag: chunks actually produced")


def test_e2e_outputs_wins_over_purpose():
    """When both are given, outputs wins (precedence), end-to-end."""
    print("test_e2e_outputs_wins_over_purpose")
    import docingest

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "kb"
        # purpose=markdown would drop chunks; outputs asks for chunks → chunks win
        docingest.ingest(_FIXTURE_DOCS / "alpha.md", output=out,
                         purpose="markdown", outputs=["markdown", "chunks"],
                         config_overrides=_E2E_OVERRIDES)
        names = _listing(out)
        _check("chunks.jsonl" in names, "outputs wins → chunks.jsonl present (purpose=markdown ignored)")


def test_invalid_values_raise():
    """Garbage purpose / outputs fail fast, not silently."""
    print("test_invalid_values_raise")
    import docingest

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "kb"
        for bad in [dict(purpose="bogus"), dict(outputs=["nope"])]:
            try:
                docingest.ingest(_FIXTURE_DOCS / "alpha.md", output=out,
                                 config_overrides=_E2E_OVERRIDES, **bad)
                raise AssertionError(f"{bad} should have raised ValueError")
            except ValueError:
                print(f"  ok: {bad} raises ValueError (fail-fast)")


if __name__ == "__main__":
    test_purpose_resolution()
    test_whitelist_translation()
    test_dependency_auto_expansion()
    test_finalize_markdown_only()
    test_finalize_noop_when_no_cleanup()
    test_finalize_rag_keeps_assets_and_index()
    test_e2e_markdown_only()
    test_e2e_rag_produces_chunks()
    test_e2e_full_is_backward_compatible()
    test_e2e_incremental_repeat_markdown()
    test_e2e_agentic_with_map_dependency()
    test_e2e_outputs_knowledge_map_only()
    test_e2e_incremental_purpose_switch()
    test_e2e_outputs_wins_over_purpose()
    test_invalid_values_raise()
    print("\n=== ALL OUTPUT-CONTROL TESTS PASSED ===")
