"""
Anti-drift guard for FILE-LEVEL index fields.

The problem this catches (a real bug we hit): a file-level field that belongs
in index.json — not in every chunk — must be wired through FOUR places, and
missing any ONE fails SILENTLY (the field just never reaches index.json, no
error):

  1. FileResult dataclass        — the field is declared
  2. process_single_file         — extracted from parse_result.metadata into
                                   the FileResult
  3. _make_index_parse_result    — overlaid back onto the metadata handed to
                                   index_builder (this layer re-reads the .md
                                   frontmatter, so a non-scalar field MUST be
                                   piped explicitly — the step everyone forgets)
  4. index_builder.add_file      — written into the index entry
  + chunk metadata blacklist     — kept OUT of every chunk (file-level data
                                   keyed by page would bloat chunks.jsonl)

This test pins all five together. The SOURCE OF TRUTH is the overlay block in
_make_index_parse_result (the `metadata["X"] = file_result.X` lines) — that is
the one place a developer MUST touch to add a file-level field, so we derive
the field set from there and assert the other four locations agree. Add a new
file-level field, forget to wire one place → this test goes red and names the
gap. Same spirit as test_command_catalog.py (pin docs to code).

Run:
    python tests/unit/test_index_file_level_fields.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

PIPELINE = ROOT / "src" / "docingest" / "pipeline.py"
INDEX_BUILDER = ROOT / "src" / "docingest" / "output" / "index_builder.py"


def _source_of_truth_fields(pipeline_src: str) -> set[str]:
    """The file-level field set = the overlay lines in _make_index_parse_result:
        metadata["X"] = file_result.X
    This is the canonical 'add a file-level field here' site."""
    fields = set(re.findall(
        r'metadata\[\s*"(\w+)"\s*\]\s*=\s*file_result\.(\w+)', pipeline_src
    ))
    # Each match is (key, attr); they must be the same name. Return the keys.
    names = set()
    for key, attr in fields:
        assert key == attr, (
            f"overlay mismatch: metadata['{key}'] = file_result.{attr} — "
            f"the index key and the FileResult attr must share one name."
        )
        names.add(key)
    assert names, (
        "Found no `metadata[\"X\"] = file_result.X` overlay lines in "
        "_make_index_parse_result. Did the function move/rename? This guard "
        "relies on that block as the source of truth."
    )
    return names


def test_file_level_fields_wired_everywhere():
    print("=== test_file_level_fields_wired_everywhere ===")
    pipeline_src = PIPELINE.read_text(encoding="utf-8")
    index_src = INDEX_BUILDER.read_text(encoding="utf-8")

    fields = _source_of_truth_fields(pipeline_src)
    print(f"  file-level index fields (from overlay): {sorted(fields)}")

    # 1. Declared on FileResult — `name: dict[str, Any] | None = None`
    declared = set(re.findall(r"^\s*(\w+):\s*dict\[str,\s*Any\]\s*\|\s*None",
                              pipeline_src, re.M))
    missing_decl = sorted(f for f in fields if f not in declared)
    assert not missing_decl, (
        f"FileResult is missing dataclass field(s): {missing_decl}. "
        f"Declare them as `<name>: dict[str, Any] | None = None`."
    )

    # 2. Extracted from metadata into FileResult in process_single_file —
    #    `result.X = ...` paired with a metadata.get("X") nearby. We check the
    #    assignment `result.<field> =` exists.
    extracted = set(re.findall(r"result\.(\w+)\s*=", pipeline_src))
    missing_extract = sorted(f for f in fields if f not in extracted)
    assert not missing_extract, (
        f"process_single_file never extracts {missing_extract} from "
        f"parse_result.metadata into the FileResult (`result.<field> = "
        f"parse_result.metadata.get(...)`). The field will be None forever."
    )

    # 4. Written into the index entry by index_builder —
    #    `entry["X"] = metadata["X"]`
    written = set(re.findall(r'entry\[\s*"(\w+)"\s*\]\s*=\s*metadata\[', index_src))
    missing_write = sorted(f for f in fields if f not in written)
    assert not missing_write, (
        f"index_builder.add_file never writes {missing_write} into the index "
        f"entry (`entry[\"<field>\"] = metadata[\"<field>\"]`). The field "
        f"reaches add_file's metadata but is dropped on the floor."
    )

    # 5. Present in the chunk metadata blacklist (kept out of every chunk).
    #    The blacklist is a frozenset of bare string literals.
    bl_match = re.search(
        r"_CHUNK_METADATA_BLACKLIST\s*=\s*frozenset\(\{(.*?)\}\)",
        pipeline_src, re.S,
    )
    assert bl_match, "could not locate _CHUNK_METADATA_BLACKLIST frozenset."
    blacklist = set(re.findall(r'"(\w+)"', bl_match.group(1)))
    missing_bl = sorted(f for f in fields if f not in blacklist)
    assert not missing_bl, (
        f"file-level field(s) {missing_bl} are NOT in _CHUNK_METADATA_BLACKLIST "
        f"— they will be copied into every chunk, bloating chunks.jsonl. Add "
        f"them to the blacklist."
    )

    print(f"  all {len(fields)} field(s) wired through all 4 sites + blacklist  PASSED\n")


def test_page_image_paths_wired():
    """page_image_paths takes a DIFFERENT route than element_boxes/page_sizes:
    it's collected from the assets on disk (format-agnostic) rather than piped
    through FileResult. Guard that route's links so it can't silently break:
      1. _collect_page_images_for_file exists in pipeline
      2. _make_index_parse_result calls it and stores into metadata
      3. index_builder writes entry["page_image_paths"]
      4. it's in the chunk blacklist (keep out of chunks.jsonl)
    """
    print("=== test_page_image_paths_wired ===")
    pipeline_src = PIPELINE.read_text(encoding="utf-8")
    index_src = INDEX_BUILDER.read_text(encoding="utf-8")

    assert "def _collect_page_images_for_file" in pipeline_src, (
        "the disk-scan collector _collect_page_images_for_file is gone — "
        "page_image_paths can no longer be built format-agnostically."
    )
    assert "_collect_page_images_for_file(" in pipeline_src.split(
        "def _collect_page_images_for_file", 1
    )[1] or 'metadata["page_image_paths"] = page_images' in pipeline_src, (
        "_make_index_parse_result no longer calls the collector / stores "
        "page_image_paths into the index metadata."
    )
    assert 'entry["page_image_paths"] = metadata[' in index_src, (
        "index_builder.add_file no longer writes page_image_paths into the "
        "index entry — it would silently vanish from index.json."
    )
    bl_match = re.search(
        r"_CHUNK_METADATA_BLACKLIST\s*=\s*frozenset\(\{(.*?)\}\)",
        pipeline_src, re.S,
    )
    assert bl_match and '"page_image_paths"' in bl_match.group(1), (
        "page_image_paths is not in _CHUNK_METADATA_BLACKLIST — it would be "
        "copied into every chunk."
    )
    print("  page_image_paths disk-scan route fully wired  PASSED\n")


def main():
    test_file_level_fields_wired_everywhere()
    test_page_image_paths_wired()
    print("ALL index-file-level-field anti-drift TESTS PASSED")


if __name__ == "__main__":
    main()
