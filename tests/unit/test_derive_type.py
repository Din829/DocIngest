# -*- coding: utf-8 -*-
"""Unit tests for derive_type_hook — semantic `type` frontmatter field.

Covers: default format→type mapping, config override (flexibility), fallback
for unknown formats, the enable/disable switch, and that an already-set type
is never clobbered. Tests the hook directly (no full pipeline) for speed.
"""

import pytest

from docingest.hooks import HookNoOp
from docingest.hooks.derive_type import derive_type_hook
from docingest.parsers.base import ParseResult


def _pr(fmt):
    return ParseResult(markdown="x", metadata={"format": fmt} if fmt else {})


def test_default_mapping():
    pr = _pr("pdf")
    derive_type_hook(__file__, pr, {})
    assert pr.metadata["type"] == "Document"

    pr = _pr("xlsx")
    derive_type_hook(__file__, pr, {})
    assert pr.metadata["type"] == "Spreadsheet"

    pr = _pr("m4a")
    derive_type_hook(__file__, pr, {})
    assert pr.metadata["type"] == "Transcript"


def test_unknown_format_uses_fallback():
    pr = _pr("dwg")  # not in the built-in table
    derive_type_hook(__file__, pr, {})
    assert pr.metadata["type"] == "Document"  # default fallback


def test_config_mapping_overrides():
    pr = _pr("xlsx")
    cfg = {"output": {"derived_metadata": {"type": {"mapping": {"xlsx": "Spec"}}}}}
    derive_type_hook(__file__, pr, cfg)
    assert pr.metadata["type"] == "Spec"


def test_config_fallback_overrides():
    pr = _pr("dwg")
    cfg = {"output": {"derived_metadata": {"type": {"fallback": "Asset"}}}}
    derive_type_hook(__file__, pr, cfg)
    assert pr.metadata["type"] == "Asset"


def test_case_insensitive_format_lookup():
    pr = ParseResult(markdown="x", metadata={"format": "PDF"})
    derive_type_hook(__file__, pr, {})
    assert pr.metadata["type"] == "Document"


def test_disabled_raises_noop():
    pr = _pr("pdf")
    cfg = {"output": {"derived_metadata": {"type": {"enabled": False}}}}
    with pytest.raises(HookNoOp):
        derive_type_hook(__file__, pr, cfg)
    assert "type" not in pr.metadata


def test_existing_type_not_clobbered():
    pr = ParseResult(markdown="x", metadata={"format": "pdf", "type": "Contract"})
    with pytest.raises(HookNoOp):
        derive_type_hook(__file__, pr, {})
    assert pr.metadata["type"] == "Contract"  # preserved


def test_no_format_raises_noop():
    pr = _pr(None)
    with pytest.raises(HookNoOp):
        derive_type_hook(__file__, pr, {})
    assert "type" not in pr.metadata
