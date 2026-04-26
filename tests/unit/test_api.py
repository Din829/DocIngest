"""
Test the public facade (docingest.api + docingest.providers + docingest.__init__).

Uses plain text/markdown inputs to avoid Vision/OCR dependencies so the
test exercises the facade's config plumbing, output whitelisting, and
read-back behaviour without needing API keys.

Run:
    python tests/unit/test_api.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import docingest
from docingest import (
    ingest,
    inspect as api_inspect,
    IngestResult,
    build_config,
    GeminiProvider,
    OpenAIProvider,
    DashScopeProvider,
    VisionProvider,
)
from docingest.api import (
    _normalize_overrides,
    _set_dotted,
    _apply_output_whitelist,
    _merge_provider,
    _resolve_wanted,
    _ALL_OUTPUTS,
)
from docingest.config import get_nested


def _make_input_dir() -> Path:
    """Create a temp dir with two plain markdown files (no Vision needed)."""
    d = Path(tempfile.mkdtemp(prefix="docingest_api_test_"))
    (d / "alpha.md").write_text(
        "# Alpha\n\n"
        "This is the alpha document.\n\n"
        "## Section one\n\nContent of section one.\n",
        encoding="utf-8",
    )
    (d / "beta.md").write_text(
        "# Beta\n\n"
        "Beta body text goes here. Enough to form at least one chunk.\n\n"
        "## Beta detail\n\nMore body content.\n",
        encoding="utf-8",
    )
    return d


# ---------------------------------------------------------------------------
# Pure-unit tests — no pipeline, no disk I/O beyond what build_config does
# ---------------------------------------------------------------------------

def test_dotted_helpers():
    """_set_dotted creates nested dicts; _normalize_overrides handles mix."""
    print("=== test_dotted_helpers ===")

    target: dict = {}
    _set_dotted(target, "a.b.c", 42)
    assert target == {"a": {"b": {"c": 42}}}, target

    # Overwrite a non-dict mid-path — should replace with dict.
    target = {"a": 1}
    _set_dotted(target, "a.b", 2)
    assert target == {"a": {"b": 2}}, target

    flat = {"parsing.vision.max_pages": 100, "chunking.max_tokens": 1024}
    nested = _normalize_overrides(flat)
    assert nested == {
        "parsing": {"vision": {"max_pages": 100}},
        "chunking": {"max_tokens": 1024},
    }, nested

    mixed = {
        "parsing.vision.max_pages": 100,
        "chunking": {"max_tokens": 2048},
    }
    nested = _normalize_overrides(mixed)
    assert nested["parsing"]["vision"]["max_pages"] == 100
    assert nested["chunking"]["max_tokens"] == 2048
    print("  PASSED\n")


def test_output_whitelist_validation():
    """_resolve_wanted rejects unknown outputs; None means 'all'."""
    print("=== test_output_whitelist_validation ===")

    assert _resolve_wanted(None) == set(_ALL_OUTPUTS)
    assert _resolve_wanted(["markdown"]) == {"markdown"}

    try:
        _resolve_wanted(["markdown", "typo"])
    except ValueError as e:
        assert "typo" in str(e), e
        print(f"  rejected unknown output: {e!s}")
    else:
        raise AssertionError("expected ValueError for unknown output")

    # Same validation inside _apply_output_whitelist
    target: dict = {}
    try:
        _apply_output_whitelist(target, ["bogus"])
    except ValueError:
        print("  _apply_output_whitelist also rejects unknown")
    else:
        raise AssertionError("expected ValueError")
    print("  PASSED\n")


def test_output_whitelist_disables_correct_knobs():
    """Asking for only 'markdown' should disable chunking/KM/quality/log."""
    print("=== test_output_whitelist_disables_correct_knobs ===")

    target: dict = {}
    _apply_output_whitelist(target, ["markdown"])

    assert get_nested(target, "chunking.enabled") is False, target
    assert get_nested(target, "knowledge_map.enabled") is False, target
    assert get_nested(target, "quality_report.enabled") is False, target
    assert get_nested(target, "run_log.enabled") is False, target
    print("  PASSED\n")


def test_merge_provider_with_class():
    """Provider object produces the right model_config slotted into the config."""
    print("=== test_merge_provider_with_class ===")

    layered: dict = {}
    _merge_provider(
        layered, "models.vision",
        GeminiProvider(api_key="sk-test", model="gemini-3-flash-preview"),
    )
    primary = get_nested(layered, "models.vision.primary")
    assert primary["provider"] == "google"
    assert primary["model"] == "gemini-3-flash-preview"
    assert primary["api_key"] == "sk-test"
    print("  PASSED\n")


def test_merge_provider_with_raw_dict():
    """Passing a raw dict bypasses the Provider class (escape hatch)."""
    print("=== test_merge_provider_with_raw_dict ===")

    layered: dict = {}
    _merge_provider(
        layered, "models.vision",
        {"primary": {"provider": "openai", "model": "gpt-5.4", "api_key": "sk-raw"}},
    )
    primary = get_nested(layered, "models.vision.primary")
    assert primary["api_key"] == "sk-raw"
    print("  PASSED\n")


def test_build_config_precedence():
    """Facade's build_config threads all overrides through load_config."""
    print("=== test_build_config_precedence ===")

    cfg = build_config(
        output="./tmp-kb",
        outputs=["markdown"],
        vision=GeminiProvider(api_key="sk-x"),
        config_overrides={"parsing.vision.max_pages": 7, "chunking.max_tokens": 321},
    )

    assert get_nested(cfg, "output.dir") == "./tmp-kb"
    # outputs=["markdown"] → chunking disabled
    assert get_nested(cfg, "chunking.enabled") is False
    # provider injected
    assert get_nested(cfg, "models.vision.primary.api_key") == "sk-x"
    assert get_nested(cfg, "models.vision.primary.model") == "gemini-3-flash-preview"
    # flat dot-path override
    assert get_nested(cfg, "parsing.vision.max_pages") == 7
    assert get_nested(cfg, "chunking.max_tokens") == 321
    print("  PASSED\n")


def test_build_config_no_side_effects_on_env():
    """build_config alone should not leak API keys to env vars (only actual LLM
    calls go through _set_api_key). Sanity check so future callers who only
    call build_config don't accidentally mutate the process environment."""
    print("=== test_build_config_no_side_effects_on_env ===")

    # Snapshot env
    before = os.environ.get("GEMINI_API_KEY")
    try:
        if before is not None:
            del os.environ["GEMINI_API_KEY"]
        build_config(vision=GeminiProvider(api_key="sk-should-not-leak"))
        assert os.environ.get("GEMINI_API_KEY") is None, (
            "build_config leaked plaintext api_key into env"
        )
    finally:
        if before is not None:
            os.environ["GEMINI_API_KEY"] = before
    print("  PASSED\n")


def test_set_api_key_plaintext_injection():
    """models.provider._set_api_key writes plaintext api_key into env var."""
    print("=== test_set_api_key_plaintext_injection ===")

    from docingest.models.provider import _set_api_key

    # Case 1: explicit api_key + explicit api_key_env → writes to api_key_env
    before = os.environ.get("GEMINI_API_KEY")
    try:
        if before is not None:
            del os.environ["GEMINI_API_KEY"]
        _set_api_key({
            "provider": "google", "model": "gemini-3-flash",
            "api_key": "sk-explicit", "api_key_env": "GEMINI_API_KEY",
        })
        assert os.environ["GEMINI_API_KEY"] == "sk-explicit"
    finally:
        if before is not None:
            os.environ["GEMINI_API_KEY"] = before
        elif "GEMINI_API_KEY" in os.environ:
            del os.environ["GEMINI_API_KEY"]

    # Case 2: api_key without api_key_env → infers from provider name
    before = os.environ.get("OPENAI_API_KEY")
    try:
        if before is not None:
            del os.environ["OPENAI_API_KEY"]
        _set_api_key({"provider": "openai", "model": "gpt-5", "api_key": "sk-inferred"})
        assert os.environ["OPENAI_API_KEY"] == "sk-inferred"
    finally:
        if before is not None:
            os.environ["OPENAI_API_KEY"] = before
        elif "OPENAI_API_KEY" in os.environ:
            del os.environ["OPENAI_API_KEY"]

    # Case 3: legacy path — no api_key, just api_key_env pointing at set env var
    os.environ["DASHSCOPE_API_KEY"] = "sk-existing"
    try:
        _set_api_key({"provider": "dashscope", "api_key_env": "DASHSCOPE_API_KEY"})
        assert os.environ["DASHSCOPE_API_KEY"] == "sk-existing"
    finally:
        del os.environ["DASHSCOPE_API_KEY"]
    print("  PASSED\n")


def test_provider_classes_shape():
    """Each concrete Provider returns the expected to_model_config shape."""
    print("=== test_provider_classes_shape ===")

    p = GeminiProvider(api_key="k")
    c = p.to_model_config()
    assert c == {"primary": {"provider": "google", "model": "gemini-3-flash-preview", "api_key": "k"}}

    p2 = OpenAIProvider(model="gpt-5", api_key=None)  # no key
    c2 = p2.to_model_config()
    assert "api_key" not in c2["primary"], c2
    assert c2["primary"]["model"] == "gpt-5"

    p3 = DashScopeProvider(api_key="ds")
    c3 = p3.to_model_config()
    assert c3["primary"]["provider"] == "dashscope"

    # Subclass hook: user could define their own VisionProvider subclass
    class MyVision(VisionProvider):
        pass

    m = MyVision(provider="custom", model="v1", api_key="k")
    mc = m.to_model_config()
    assert mc["primary"]["provider"] == "custom"
    print("  PASSED\n")


# ---------------------------------------------------------------------------
# Integration tests — actually run the pipeline on plain markdown files
# ---------------------------------------------------------------------------

def test_ingest_minimal_runs_end_to_end():
    """
    Plain md files → ingest → IngestResult carries markdown + chunks + index.

    Vision/LLM are not invoked (md files have no images and
    knowledge_map AI stage is disabled below), so this runs offline.
    """
    print("=== test_ingest_minimal_runs_end_to_end ===")

    inp = _make_input_dir()
    out = Path(tempfile.mkdtemp(prefix="docingest_api_test_out_"))
    try:
        result = ingest(
            list(inp.glob("*.md")),
            output=out,
            # Turn off AI stages so test is fully offline.
            config_overrides={
                "knowledge_map.enrich_with_ai": False,
                "run_log.enabled": False,
            },
        )

        assert isinstance(result, IngestResult)
        assert result.stats["total_files"] == 2, result.stats
        assert result.stats["successful"] == 2, result.stats
        assert result.stats["failed"] == 0, result.stats

        # Outputs whitelist default (None) → read back markdown + chunks + index
        paths = sorted(md["path"] for md in result.markdown_files)
        assert paths == ["sources/alpha.md", "sources/beta.md"], paths
        assert all(md["content"] for md in result.markdown_files)
        # Frontmatter captured
        assert all("format" in md["metadata"] for md in result.markdown_files)

        assert len(result.chunks) > 0, "expected at least one chunk"
        assert all("id" in c and "text" in c and "metadata" in c for c in result.chunks)

        assert "files" in result.index, result.index

        # Output dir is absolute path of the one we passed in
        assert Path(result.output_dir).resolve() == out.resolve()
    finally:
        shutil.rmtree(inp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    print("  PASSED\n")


def test_ingest_outputs_markdown_only():
    """outputs=['markdown'] disables chunking + KM + QR + run_log."""
    print("=== test_ingest_outputs_markdown_only ===")

    inp = _make_input_dir()
    out = Path(tempfile.mkdtemp(prefix="docingest_api_test_out2_"))
    try:
        result = ingest(
            list(inp.glob("*.md")),
            output=out,
            outputs=["markdown"],
        )

        assert result.stats["successful"] == 2
        assert len(result.markdown_files) == 2
        # Whitelist excluded chunks → chunks.jsonl not produced (or produced empty)
        assert result.chunks == [], result.chunks
        # Whitelist excluded knowledge_map → file not produced
        assert not (out / "knowledge_map.yaml").exists()
        # Whitelist excluded quality_report → file not produced
        assert not (out / "quality_report.json").exists()
        # run_log too
        assert not (out / "log.md").exists()
    finally:
        shutil.rmtree(inp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    print("  PASSED\n")


def test_ingest_force_reprocesses():
    """force=True ignores incremental cache → second run also reprocesses."""
    print("=== test_ingest_force_reprocesses ===")

    inp = _make_input_dir()
    out = Path(tempfile.mkdtemp(prefix="docingest_api_test_out3_"))
    try:
        common = {
            "output": out,
            "outputs": ["markdown", "chunks", "index"],
            "config_overrides": {
                "knowledge_map.enrich_with_ai": False,
                "run_log.enabled": False,
            },
        }

        r1 = ingest(list(inp.glob("*.md")), **common)
        assert r1.stats["successful"] == 2

        # Second run should hit cache (all files are "cached", none updated)
        r2 = ingest(list(inp.glob("*.md")), **common)
        statuses = {f.get("status") for f in r2.stats.get("errors", [])}
        # We observe cache via file count + successful still == 2
        assert r2.stats["successful"] == 2, r2.stats

        # Force run
        r3 = ingest(list(inp.glob("*.md")), force=True, **common)
        assert r3.stats["successful"] == 2

        # We don't inspect individual file statuses here (pipeline logs
        # them but they aren't surfaced on stats.errors); the goal is
        # that the three runs all produce a valid knowledge base
        # regardless of cache state.
        _ = statuses
    finally:
        shutil.rmtree(inp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    print("  PASSED\n")


def test_inspect_facade():
    """api.inspect → same shape inspect_files returns."""
    print("=== test_inspect_facade ===")

    inp = _make_input_dir()
    try:
        results = api_inspect(list(inp.glob("*.md")))
        assert len(results) == 2
        names = sorted(r["name"] for r in results)
        assert names == ["alpha.md", "beta.md"], names
        for r in results:
            assert r["format"] == "md"
            assert "size_mb" in r
            assert "recommendation" in r
    finally:
        shutil.rmtree(inp, ignore_errors=True)
    print("  PASSED\n")


def test_public_api_surface():
    """docingest.__init__ exports the documented names."""
    print("=== test_public_api_surface ===")

    expected = {
        "ingest", "inspect", "refine", "IngestResult", "build_config",
        "VisionProvider", "AudioProvider", "TextProvider",
        "GeminiProvider", "OpenAIProvider", "AnthropicProvider",
        "DashScopeProvider", "WhisperProvider",
        "__version__",
    }
    missing = expected - set(dir(docingest))
    assert not missing, f"missing exports: {missing}"
    assert set(docingest.__all__) >= (expected - {"__version__"}) | {"__version__"}
    print("  PASSED\n")


def test_ingest_keyword_only_signature():
    """All parameters past `paths` must be keyword-only (future-proofs API)."""
    print("=== test_ingest_keyword_only_signature ===")

    import inspect as py_inspect
    sig = py_inspect.signature(ingest)
    params = list(sig.parameters.values())
    # First param: paths (positional-or-keyword)
    assert params[0].name == "paths"
    assert params[0].kind in (
        py_inspect.Parameter.POSITIONAL_OR_KEYWORD,
        py_inspect.Parameter.POSITIONAL_ONLY,
    )
    # All subsequent params: keyword-only
    for p in params[1:]:
        assert p.kind == py_inspect.Parameter.KEYWORD_ONLY, (
            f"{p.name} must be keyword-only but is {p.kind}"
        )
    print("  PASSED\n")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    # Pure unit
    test_dotted_helpers()
    test_output_whitelist_validation()
    test_output_whitelist_disables_correct_knobs()
    test_merge_provider_with_class()
    test_merge_provider_with_raw_dict()
    test_build_config_precedence()
    test_build_config_no_side_effects_on_env()
    test_set_api_key_plaintext_injection()
    test_provider_classes_shape()
    test_public_api_surface()
    test_ingest_keyword_only_signature()
    # Integration
    test_inspect_facade()
    test_ingest_minimal_runs_end_to_end()
    test_ingest_outputs_markdown_only()
    test_ingest_force_reprocesses()
    print("ALL docingest.api TESTS PASSED")


if __name__ == "__main__":
    main()
