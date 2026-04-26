"""
Test chunk lineage — every chunk should carry a `metadata.lineage` sub-dict
with source_markdown / original_input / transformations. Uses plain Markdown
inputs so no Vision / ASR / Office-conversion dependencies kick in.

Run:
    python tests/unit/test_lineage.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import docingest
from docingest.api import IngestResult


def _make_input_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="docingest_lineage_test_"))
    (d / "alpha.md").write_text(
        "# Alpha\n\n"
        "Alpha body goes here.\n\n"
        "## Section one\n\nContent of section one.\n",
        encoding="utf-8",
    )
    (d / "beta.md").write_text(
        "# Beta\n\nBody text for beta.\n\n## Beta detail\n\nMore body content here.\n",
        encoding="utf-8",
    )
    return d


def test_every_chunk_has_lineage():
    """Basic shape — lineage exists and has the three documented keys."""
    print("=== test_every_chunk_has_lineage ===")

    inp = _make_input_dir()
    out = Path(tempfile.mkdtemp(prefix="docingest_lineage_out_"))
    try:
        result: IngestResult = docingest.ingest(
            list(inp.glob("*.md")),
            output=out,
            outputs=["markdown", "chunks", "index"],
            config_overrides={
                # Keep the run fully offline.
                "knowledge_map.enrich_with_ai": False,
                "run_log.enabled": False,
            },
        )
        assert result.stats["successful"] == 2, result.stats
        assert len(result.chunks) > 0, "need at least one chunk to test lineage"

        for chunk in result.chunks:
            meta = chunk["metadata"]
            assert "lineage" in meta, f"chunk {chunk['id']} has no lineage"
            lineage = meta["lineage"]

            # Three required sub-keys
            assert "source_markdown" in lineage, lineage
            assert "original_input" in lineage, lineage
            assert "transformations" in lineage, lineage

            # source_markdown points at sources/*.md
            assert lineage["source_markdown"].startswith("sources/"), lineage["source_markdown"]
            assert lineage["source_markdown"].endswith(".md")

            # original_input carries at least the filename of the raw input
            assert "filename" in lineage["original_input"], lineage["original_input"]
            assert lineage["original_input"]["filename"].endswith(".md")

            # transformations is an ordered list of dicts with `step`
            assert isinstance(lineage["transformations"], list)
            assert len(lineage["transformations"]) >= 2, lineage["transformations"]
            for entry in lineage["transformations"]:
                assert "step" in entry, entry
    finally:
        shutil.rmtree(inp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    print("  PASSED\n")


def test_transformations_record_parser_and_chunker():
    """
    For a minimal md input (no hooks, no Vision triggered) the provenance
    trail MUST contain exactly parser + chunker — in that order. This
    pins the ordering contract so future refactors can't silently swap
    them.
    """
    print("=== test_transformations_record_parser_and_chunker ===")

    inp = _make_input_dir()
    out = Path(tempfile.mkdtemp(prefix="docingest_lineage_out2_"))
    try:
        result = docingest.ingest(
            list(inp.glob("*.md")),
            output=out,
            outputs=["markdown", "chunks"],
            config_overrides={
                "parsing.vision.enabled": False,  # no vision entries
                "knowledge_map.enrich_with_ai": False,
                "run_log.enabled": False,
            },
        )
        assert result.stats["successful"] == 2
        assert result.chunks

        chunk = result.chunks[0]
        transforms = chunk["metadata"]["lineage"]["transformations"]
        steps = [t["step"] for t in transforms]

        # Parser must come before chunker (order-sensitive contract).
        assert "parser" in steps, steps
        assert "chunker" in steps, steps
        assert steps.index("parser") < steps.index("chunker"), steps

        # No vision / hook entries expected for plain-md, vision-disabled run.
        assert "vision" not in steps, steps

        # Parser entry has identifiable name + format
        parser_entry = next(t for t in transforms if t["step"] == "parser")
        assert "name" in parser_entry
        assert parser_entry["format"] == "md", parser_entry

        # Chunker entry has identifiable name
        chunker_entry = next(t for t in transforms if t["step"] == "chunker")
        assert chunker_entry["name"], chunker_entry
        # max_tokens is present (recorded from BaseChunker._max_tokens)
        assert "max_tokens" in chunker_entry, chunker_entry
    finally:
        shutil.rmtree(inp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    print("  PASSED\n")


def test_lineage_original_input_has_filename_not_md_path():
    """
    original_input.filename must be the INPUT file name (e.g. alpha.md),
    NOT the sources/*.md output name. This distinction is load-bearing
    for binary inputs (report.pdf vs sources/report.md) even though this
    test uses md-in md-out — the contract still holds.
    """
    print("=== test_lineage_original_input_has_filename_not_md_path ===")

    inp = _make_input_dir()
    out = Path(tempfile.mkdtemp(prefix="docingest_lineage_out3_"))
    try:
        result = docingest.ingest(
            list(inp.glob("*.md")),
            output=out,
            outputs=["chunks"],
            config_overrides={
                "parsing.vision.enabled": False,
                "knowledge_map.enrich_with_ai": False,
                "run_log.enabled": False,
            },
        )
        filenames_seen = {
            c["metadata"]["lineage"]["original_input"]["filename"]
            for c in result.chunks
        }
        # Both alpha.md and beta.md produce chunks, so we see both filenames.
        assert filenames_seen == {"alpha.md", "beta.md"}, filenames_seen

        # And source_markdown doesn't equal the input filename — they're
        # different fields carrying different things.
        for chunk in result.chunks:
            lineage = chunk["metadata"]["lineage"]
            assert lineage["source_markdown"] != lineage["original_input"]["filename"]
    finally:
        shutil.rmtree(inp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    print("  PASSED\n")


def test_legacy_flat_metadata_preserved():
    """
    Backwards compat — adding lineage must not have removed or renamed any
    of the flat metadata keys existing consumers depend on. Pins the
    promise made in the ARCHITECTURE doc.
    """
    print("=== test_legacy_flat_metadata_preserved ===")

    inp = _make_input_dir()
    out = Path(tempfile.mkdtemp(prefix="docingest_lineage_out4_"))
    try:
        result = docingest.ingest(
            list(inp.glob("*.md")),
            output=out,
            outputs=["chunks"],
            config_overrides={
                "parsing.vision.enabled": False,
                "knowledge_map.enrich_with_ai": False,
                "run_log.enabled": False,
            },
        )
        # These flat keys are the load-bearing identity / enrichment fields
        # every chunk must keep carrying regardless of lineage. `title_path`
        # is intentionally excluded — it's optional (only set for chunks
        # inside a heading section, absent for orphan prelude / short docs).
        required_flat_keys = {
            "source", "original_file", "format",
            "chunk_index", "total_chunks", "tokens",
        }
        for chunk in result.chunks:
            missing = required_flat_keys - set(chunk["metadata"].keys())
            assert not missing, f"missing flat keys {missing} in {chunk['id']}"
    finally:
        shutil.rmtree(inp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    print("  PASSED\n")


def test_disabled_hooks_not_in_lineage():
    """
    Hooks that don't actually do anything (sanitize disabled by default,
    hook raises HookNoOp) MUST NOT appear in transformations. This pins
    the "positive provenance trail" contract — lineage records what
    actually shaped the chunk, not what got called-then-declined.
    """
    print("=== test_disabled_hooks_not_in_lineage ===")

    inp = _make_input_dir()
    out = Path(tempfile.mkdtemp(prefix="docingest_lineage_out6_"))
    try:
        result = docingest.ingest(
            list(inp.glob("*.md")),
            output=out,
            outputs=["chunks"],
            config_overrides={
                "parsing.vision.enabled": False,
                # sanitize.enabled defaults to False → sanitize_hook
                # should raise HookNoOp and NOT show up in lineage.
                "knowledge_map.enrich_with_ai": False,
                "run_log.enabled": False,
            },
        )
        for chunk in result.chunks:
            transforms = chunk["metadata"]["lineage"]["transformations"]
            hook_names = [
                t["name"] for t in transforms if t["step"] == "hook"
            ]
            assert "sanitize_hook" not in hook_names, (
                f"sanitize_hook was recorded even though sanitize.enabled=False: "
                f"{hook_names}"
            )
    finally:
        shutil.rmtree(inp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    print("  PASSED\n")


def test_transformations_are_independent_across_chunks():
    """
    Each chunk's lineage.transformations must be its own list — mutating
    one must not leak into the others. This pins the copy-on-attach
    contract inside pipeline.process_single_file.
    """
    print("=== test_transformations_are_independent_across_chunks ===")

    inp = _make_input_dir()
    out = Path(tempfile.mkdtemp(prefix="docingest_lineage_out5_"))
    try:
        result = docingest.ingest(
            list(inp.glob("*.md")),
            output=out,
            outputs=["chunks"],
            config_overrides={
                "parsing.vision.enabled": False,
                "knowledge_map.enrich_with_ai": False,
                "run_log.enabled": False,
            },
        )
        chunks = result.chunks
        assert len(chunks) >= 2, "need at least two chunks to test independence"

        # Mutate chunk 0's transformations; chunk 1's must stay untouched.
        before_len = len(chunks[1]["metadata"]["lineage"]["transformations"])
        chunks[0]["metadata"]["lineage"]["transformations"].append({"step": "X"})
        after_len = len(chunks[1]["metadata"]["lineage"]["transformations"])
        assert before_len == after_len, (
            f"transformations leaked across chunks: {before_len} -> {after_len}"
        )
    finally:
        shutil.rmtree(inp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    print("  PASSED\n")


def main():
    test_every_chunk_has_lineage()
    test_transformations_record_parser_and_chunker()
    test_lineage_original_input_has_filename_not_md_path()
    test_legacy_flat_metadata_preserved()
    test_disabled_hooks_not_in_lineage()
    test_transformations_are_independent_across_chunks()
    print("ALL lineage tests PASSED")


if __name__ == "__main__":
    main()
