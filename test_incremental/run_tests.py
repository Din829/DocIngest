"""Edge-case tests for the incremental pipeline."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from docingest.config import load_config
from docingest.parsers import create_parser
from docingest.chunkers import create_chunker
from docingest.pipeline import run_pipeline

BASE = Path(__file__).resolve().parent
DOCS = BASE / "docs"
OUT = BASE / "out"


def fresh_env():
    """Reset output directory; keep docs."""
    if OUT.exists():
        shutil.rmtree(OUT)


def build_config(max_tokens: int = 512, incremental: bool = True, force: bool = False) -> dict:
    overrides = {
        "output": {"dir": str(OUT)},
        "parsing": {"vision": {"enabled": False}},
        "knowledge_map": {"enabled": False},
        "chunking": {"max_tokens": max_tokens},
        "incremental": {"enabled": incremental, "force": force},
    }
    return load_config(cli_overrides=overrides)


def run(label: str, config: dict) -> dict:
    """Run pipeline and return summary dict."""
    t0 = time.monotonic()
    parser = create_parser(config)
    chunker = create_chunker(config)
    result = run_pipeline([DOCS], config, parser, chunker)
    elapsed = time.monotonic() - t0

    # Count cached vs processed files
    cached_count = sum(1 for f in result.files if f.parse_time_ms == 0 and f.success)
    processed_count = sum(1 for f in result.files if f.parse_time_ms > 0 and f.success)

    print(f"[{label}] ok={result.successful} failed={result.failed} "
          f"chunks={result.total_chunks} "
          f"(cached={cached_count}, processed={processed_count}) "
          f"wall={elapsed:.2f}s")
    return {
        "successful": result.successful,
        "failed": result.failed,
        "chunks": result.total_chunks,
        "cached": cached_count,
        "processed": processed_count,
        "files": [(f.original_file, f.chunks_count, f.parse_time_ms) for f in result.files],
    }


def meta_count() -> int:
    cache_dir = OUT / ".cache"
    if not cache_dir.exists():
        return 0
    return len(list(cache_dir.glob("*.meta.json")))


def assert_eq(actual, expected, msg):
    if actual != expected:
        raise AssertionError(f"FAIL: {msg}\n  expected: {expected}\n  actual:   {actual}")
    print(f"  ✓ {msg}")


# ============================================================================
# Setup: initial full run
# ============================================================================
print("=" * 70)
print("SETUP: Fresh full run with 2 files")
print("=" * 70)
fresh_env()
config = build_config()
r0 = run("fresh", config)
assert_eq(r0["successful"], 2, "fresh run: 2 files successful")
assert_eq(r0["processed"], 2, "fresh run: 2 files processed (not cached)")
assert_eq(meta_count(), 2, "fresh run: 2 meta.json created")
baseline_chunks = r0["chunks"]

# ============================================================================
# Test 1: Full cache hit (repeat same input)
# ============================================================================
print()
print("=" * 70)
print("TEST 1: Repeat run — expect full cache hit")
print("=" * 70)
r1 = run("repeat", config)
assert_eq(r1["successful"], 2, "repeat: 2 files successful")
assert_eq(r1["cached"], 2, "repeat: 2 files from cache")
assert_eq(r1["processed"], 0, "repeat: 0 files processed")
assert_eq(r1["chunks"], baseline_chunks, "repeat: same chunks count")

# ============================================================================
# Test 2: File renamed (content unchanged) — should re-process
# ============================================================================
print()
print("=" * 70)
print("TEST 2: File renamed (content unchanged) — expect re-process")
print("=" * 70)
src = DOCS / "beta.md"
renamed = DOCS / "beta_renamed.md"
src.rename(renamed)
try:
    r2 = run("renamed", config)
    assert_eq(r2["successful"], 2, "renamed: 2 files successful")
    # alpha should still be cached; beta_renamed is a new name → process
    assert_eq(r2["cached"], 1, "renamed: alpha still cached")
    assert_eq(r2["processed"], 1, "renamed: beta_renamed processed as new")
    assert_eq(meta_count(), 3, "renamed: total 3 meta.json (alpha, beta old, beta_renamed)")
finally:
    renamed.rename(src)

# ============================================================================
# Test 3: Content modified — should re-process only modified file
# ============================================================================
print()
print("=" * 70)
print("TEST 3: Content modified — expect modified file re-processed")
print("=" * 70)
# Clean up meta and restart clean for this test
fresh_env()
config = build_config()
run("baseline", config)  # Create cache

original_content = (DOCS / "alpha.md").read_text(encoding="utf-8")
try:
    modified = original_content + "\n\n## New Section\nExtra content added later.\n"
    (DOCS / "alpha.md").write_text(modified, encoding="utf-8")

    r3 = run("modified", config)
    assert_eq(r3["successful"], 2, "modified: 2 files successful")
    assert_eq(r3["cached"], 1, "modified: beta still cached")
    assert_eq(r3["processed"], 1, "modified: alpha re-processed (content changed)")
finally:
    (DOCS / "alpha.md").write_text(original_content, encoding="utf-8")

# ============================================================================
# Test 4: Config changed (max_tokens) — should invalidate all
# ============================================================================
print()
print("=" * 70)
print("TEST 4: Config changed (max_tokens 512→256) — expect all re-process")
print("=" * 70)
fresh_env()
config_512 = build_config(max_tokens=512)
run("baseline-512", config_512)

config_256 = build_config(max_tokens=256)
r4 = run("config-256", config_256)
assert_eq(r4["successful"], 2, "config changed: 2 files successful")
assert_eq(r4["cached"], 0, "config changed: 0 files from cache")
assert_eq(r4["processed"], 2, "config changed: all files re-processed")

# ============================================================================
# Test 5: sources/foo.md deleted — should detect and re-process
# ============================================================================
print()
print("=" * 70)
print("TEST 5: Delete sources/alpha.md — expect alpha re-process")
print("=" * 70)
fresh_env()
config = build_config()
run("baseline", config)

(OUT / "sources" / "alpha.md").unlink()
r5 = run("md-deleted", config)
assert_eq(r5["successful"], 2, "md deleted: 2 files successful")
assert_eq(r5["cached"], 1, "md deleted: beta still cached")
assert_eq(r5["processed"], 1, "md deleted: alpha re-processed")
assert (OUT / "sources" / "alpha.md").exists(), "alpha.md should be recreated"
print("  ✓ sources/alpha.md recreated")

# ============================================================================
# Test 6: chunks.jsonl deleted but cache intact — should re-process all
# ============================================================================
print()
print("=" * 70)
print("TEST 6: Delete chunks.jsonl only — expect all re-process (chunk_ids miss)")
print("=" * 70)
fresh_env()
config = build_config()
run("baseline", config)

(OUT / "chunks.jsonl").unlink()
r6 = run("chunks-deleted", config)
assert_eq(r6["successful"], 2, "chunks deleted: 2 files successful")
assert_eq(r6["cached"], 0, "chunks deleted: 0 cached (chunk_ids missing)")
assert_eq(r6["processed"], 2, "chunks deleted: all re-processed")
assert (OUT / "chunks.jsonl").exists(), "chunks.jsonl should be recreated"
print("  ✓ chunks.jsonl recreated")

# ============================================================================
# Test 7: incremental.enabled = false — should behave like full rebuild
# ============================================================================
print()
print("=" * 70)
print("TEST 7: incremental.enabled=false — expect no cache usage")
print("=" * 70)
fresh_env()
config_on = build_config(incremental=True)
run("baseline", config_on)

config_off = build_config(incremental=False)
r7 = run("incremental-off", config_off)
assert_eq(r7["successful"], 2, "incremental off: 2 files successful")
# With incremental disabled, should not check cache → all processed
assert_eq(r7["processed"], 2, "incremental off: all files processed")
assert_eq(r7["cached"], 0, "incremental off: 0 cached")

# ============================================================================
# Test 8: Mixed scenario — cached + new + modified
# ============================================================================
print()
print("=" * 70)
print("TEST 8: Mixed — cached (alpha) + new (gamma) + modified (beta)")
print("=" * 70)
fresh_env()
config = build_config()
run("baseline-2files", config)

# Add gamma (new), modify beta
(DOCS / "gamma.md").write_text(
    "# Gamma\n\n## Intro\nBrand new document with enough content to form chunks.\n"
    "More lines here to ensure substance.\n",
    encoding="utf-8",
)
beta_orig = (DOCS / "beta.md").read_text(encoding="utf-8")
try:
    (DOCS / "beta.md").write_text(
        beta_orig + "\n\n## Added\nSomething extra in beta now.\n",
        encoding="utf-8",
    )

    r8 = run("mixed", config)
    assert_eq(r8["successful"], 3, "mixed: 3 files successful")
    assert_eq(r8["cached"], 1, "mixed: 1 cached (alpha)")
    assert_eq(r8["processed"], 2, "mixed: 2 processed (beta modified + gamma new)")
finally:
    (DOCS / "beta.md").write_text(beta_orig, encoding="utf-8")
    (DOCS / "gamma.md").unlink(missing_ok=True)

# ============================================================================
# Summary
# ============================================================================
print()
print("=" * 70)
print("ALL TESTS PASSED ✓")
print("=" * 70)
