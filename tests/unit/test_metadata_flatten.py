"""
Tests for utils.metadata_flatten.flatten_metadata.

The fixtures mirror REAL chunk metadata observed end-to-end (a docx/xlsx run):
the nested `lineage` dict (with a list-of-dicts `transformations` and an
18-digit `binary_hash`), the per-format `*_visuals` dict, plus the ordinary
flat fields. The core assertion is the contract verified against a live
Pinecone index: after flattening, EVERY value must be str / number / bool /
list[str].
"""

from __future__ import annotations

import json

import pytest

from docingest.utils.metadata_flatten import flatten_metadata, _SAFE_INT_MAX


# A faithful copy of real chunk metadata (xlsx + docx shapes merged so one
# fixture exercises lineage, transformations list-of-dicts, both *_visuals,
# the big binary_hash, and the flat fields).
REAL_METADATA = {
    "source": "knowledge/x/sources/plan.md",
    "original_file": "plan.xlsx",
    "format": "xlsx",
    "title": "plan",
    "language": "ja",
    "type": "Document",
    "last_modified": "2026-06-24T19:44:10",
    "sheet_name": "Sheet1",
    "title_path": "plan > Sheet1",
    "chunk_index": 0,
    "total_chunks": 3,
    "tokens": 474,
    "has_table": True,
    "has_image_ref": False,
    # nested dict
    "xlsx_sheet_visuals": {"Sheet1": 0, "Sheet2": 2},
    # nested dict with a sub-dict AND a list-of-dicts, plus a big int
    "lineage": {
        "source_markdown": "sources/plan.md",
        "original_input": {
            "filename": "plan.xlsx",
            "mimetype": "application/vnd.openxmlformats",
            "binary_hash": 11017582214739349844,  # 18 digits, > 2**53
        },
        "transformations": [
            {"step": "parser", "name": "_DoclingWithFallback", "format": "xlsx"},
            {"step": "hook", "name": "file_metadata_hook", "phase": "pre_write"},
        ],
    },
}

PINECONE_OK = (str, int, float, bool)


def _assert_strict_store_compatible(meta: dict) -> None:
    """Every value must be str / number / bool / list[str] — the verified
    Pinecone contract. No nested dicts, no lists-of-non-strings."""
    for k, v in meta.items():
        if isinstance(v, list):
            assert all(isinstance(x, str) for x in v), (
                f"{k}: list must be list[str], got {v!r}"
            )
        else:
            assert isinstance(v, PINECONE_OK) and not isinstance(v, dict), (
                f"{k}: value must be str/number/bool, got {type(v).__name__}"
            )


def test_flatten_mode_is_strict_store_compatible():
    flat = flatten_metadata(REAL_METADATA)  # default mode="flatten"
    _assert_strict_store_compatible(flat)


def test_drop_mode_is_strict_store_compatible():
    flat = flatten_metadata(REAL_METADATA, mode="drop")
    _assert_strict_store_compatible(flat)


def test_flatten_preserves_nested_via_dotted_keys():
    flat = flatten_metadata(REAL_METADATA)
    # nested scalars surface as dotted keys
    assert flat["lineage.source_markdown"] == "sources/plan.md"
    assert flat["lineage.original_input.filename"] == "plan.xlsx"
    assert flat["xlsx_sheet_visuals.Sheet1"] == 0
    assert flat["xlsx_sheet_visuals.Sheet2"] == 2
    # the original nested key is gone
    assert "lineage" not in flat
    assert "xlsx_sheet_visuals" not in flat


def test_flatten_list_of_dicts_becomes_json_string():
    flat = flatten_metadata(REAL_METADATA)
    # transformations is a list of dicts → JSON-encoded, no data lost
    raw = flat["lineage.transformations"]
    assert isinstance(raw, str)
    decoded = json.loads(raw)
    assert decoded[0]["name"] == "_DoclingWithFallback"
    assert len(decoded) == 2


def test_big_int_is_stringified():
    flat = flatten_metadata(REAL_METADATA)
    bh = flat["lineage.original_input.binary_hash"]
    assert isinstance(bh, str)
    assert bh == "11017582214739349844"


def test_big_int_can_be_kept_numeric_when_disabled():
    flat = flatten_metadata(REAL_METADATA, stringify_big_ints=False)
    bh = flat["lineage.original_input.binary_hash"]
    assert isinstance(bh, int)
    assert bh == 11017582214739349844


def test_in_range_int_stays_numeric():
    flat = flatten_metadata(REAL_METADATA)
    # tokens / chunk_index are small ints — must remain ints, not stringified
    assert flat["tokens"] == 474 and isinstance(flat["tokens"], int)
    assert flat["chunk_index"] == 0 and isinstance(flat["chunk_index"], int)


def test_drop_mode_removes_nested_keeps_flat():
    flat = flatten_metadata(REAL_METADATA, mode="drop")
    # nested gone entirely
    assert not any(k.startswith("lineage") for k in flat)
    assert not any(k.startswith("xlsx_sheet_visuals") for k in flat)
    # flat scalars retained
    assert flat["source"] == "knowledge/x/sources/plan.md"
    assert flat["tokens"] == 474
    assert flat["has_table"] is True


def test_bool_not_treated_as_int():
    # bool is an int subclass — must NOT be stringified even though the rule
    # touches ints. Verify True/False pass through as bools.
    m = {"flag_t": True, "flag_f": False}
    flat = flatten_metadata(m)
    assert flat["flag_t"] is True
    assert flat["flag_f"] is False


def test_list_of_strings_preserved():
    m = {"keywords": ["a", "b", "c"]}
    flat = flatten_metadata(m)
    assert flat["keywords"] == ["a", "b", "c"]


def test_custom_separator():
    flat = flatten_metadata(REAL_METADATA, sep="__")
    assert flat["lineage__source_markdown"] == "sources/plan.md"
    assert "lineage__original_input__filename" in flat


def test_input_not_mutated():
    import copy
    before = copy.deepcopy(REAL_METADATA)
    flatten_metadata(REAL_METADATA)
    assert REAL_METADATA == before, "flatten_metadata must not mutate its input"


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        flatten_metadata(REAL_METADATA, mode="nonsense")


def test_empty_metadata():
    assert flatten_metadata({}) == {}


def test_safe_int_boundary():
    # exactly 2**53 is "in range" (> check), one above is stringified
    m = {"at_limit": _SAFE_INT_MAX, "over_limit": _SAFE_INT_MAX + 1}
    flat = flatten_metadata(m)
    assert isinstance(flat["at_limit"], int)
    assert isinstance(flat["over_limit"], str)
