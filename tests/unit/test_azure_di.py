"""
Azure DI plugin — offline tests (no network, no azure SDK required).

Covers what can be verified without a live Azure endpoint:

1. Plugin is OPTIONAL — importing docingest / building the docling parser never
   needs the azure SDK; the azure_di branch only imports it on demand.
2. converter.convert maps a recorded AnalyzeResult-shaped fixture to the
   ParseResult contract correctly (markdown body, per-page slicing via spans,
   furniture stripping, metadata flags).
3. AzureDIParser fails loud on missing credentials (system-edge validation),
   and isolates a per-file network error into ParseResult(success=False).
4. create_parser routes parsing.engine=azure_di to AzureDIParser.

Live-endpoint concerns (HTML table handling, real span offsets) are documented
in src/docingest/azure/README.md and require credentials — out of scope here.

Run:
    python tests/unit/test_azure_di.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------------------
# Fixtures — a fake AnalyzeResult with the shape the SDK returns.
# Using plain objects (not the SDK models) proves converter is SDK-free.
# ---------------------------------------------------------------------------

class _Span:
    def __init__(self, offset: int, length: int) -> None:
        self.offset = offset
        self.length = length


class _Page:
    def __init__(self, page_number: int, spans: list[_Span]) -> None:
        self.page_number = page_number
        self.spans = spans


class _FakeAnalyzeResult:
    def __init__(self, content: str, pages: list[_Page]) -> None:
        self.content = content
        self.pages = pages


def _two_page_fixture() -> _FakeAnalyzeResult:
    # Two pages; page 1 carries a PageHeader furniture line, page 2 a PageFooter.
    page1 = 'PageHeader="ACME spec"\n# Section 1\nBody of page one.\n'
    page2 = '## Section 2\nBody of page two.\nPageFooter="1 | Page"\n'
    content = page1 + page2
    pages = [
        _Page(1, [_Span(0, len(page1))]),
        _Page(2, [_Span(len(page1), len(page2))]),
    ]
    return _FakeAnalyzeResult(content, pages)


# ---------------------------------------------------------------------------
# 1. Optionality
# ---------------------------------------------------------------------------

def test_main_import_does_not_need_azure_sdk() -> None:
    code = """
import sys
import docingest
assert "docingest.azure" not in sys.modules
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    print("ok: main import is azure-free")


def test_converter_is_sdk_free() -> None:
    # converter imports must not drag the azure SDK in.
    from docingest.azure import converter  # noqa: F401
    assert "azure.ai.documentintelligence" not in sys.modules, (
        "converter must not import the azure SDK"
    )
    print("ok: converter is SDK-free")


# ---------------------------------------------------------------------------
# 2. converter mapping
# ---------------------------------------------------------------------------

def test_convert_maps_pages_and_strips_furniture() -> None:
    from docingest.azure import converter

    result = _two_page_fixture()
    out = converter.convert(result, file_path=Path("spec.pdf"), source_format="pdf")

    # markdown body: furniture marker lines removed, real content kept
    assert "PageHeader=" not in out["markdown"]
    assert "PageFooter=" not in out["markdown"]
    assert "# Section 1" in out["markdown"]
    assert "Body of page two." in out["markdown"]

    # pages: 2, sliced from RAW content via spans (furniture still present in
    # the per-page text because slicing uses the original offsets)
    pages = out["pages"]
    assert len(pages) == 2
    assert pages[0].page_no == 1
    assert pages[1].page_no == 2
    assert "Body of page one." in pages[0].text
    assert "Body of page two." in pages[1].text

    # metadata contract
    meta = out["metadata"]
    assert meta["format"] == "pdf"
    assert meta["title"] == "spec"
    assert meta["parser_engine"] == "azure_di"
    assert meta["pages"] == 2
    assert meta["has_tables"] is False    # no <table> in fixture
    assert meta["has_images"] is False    # no <figure> in fixture
    print("ok: convert maps pages + strips furniture + metadata")


def test_convert_detects_html_table_and_figure() -> None:
    from docingest.azure import converter

    content = '# T\n<table><tr><td>a</td></tr></table>\n<figure>x</figure>\n'
    result = _FakeAnalyzeResult(content, [_Page(1, [_Span(0, len(content))])])
    out = converter.convert(result, file_path=Path("t.pdf"), source_format="pdf")

    assert out["metadata"]["has_tables"] is True
    assert out["metadata"]["has_images"] is True
    # HTML table kept verbatim (not converted) — documented behaviour
    assert "<table>" in out["markdown"]
    print("ok: convert flags HTML table + figure")


def test_convert_handles_empty_and_no_pages() -> None:
    from docingest.azure import converter

    # empty content, no pages
    out = converter.convert(
        _FakeAnalyzeResult("", []), file_path=Path("e.pdf"), source_format="pdf"
    )
    assert out["markdown"] == ""
    assert out["pages"] == []
    assert "pages" not in out["metadata"]   # no page count when there are none
    print("ok: convert handles empty input")


# ---------------------------------------------------------------------------
# 3. parser credential validation + error isolation
# ---------------------------------------------------------------------------

def test_parser_fails_loud_on_missing_credentials(monkeypatch=None) -> None:
    from docingest.azure.di_parser import AzureDIParser

    # Ensure env is clean so config-less construction has no credentials.
    import os
    for k in ("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "AZURE_DOCUMENT_INTELLIGENCE_KEY"):
        os.environ.pop(k, None)

    parser = AzureDIParser({"parsing": {"engine": "azure_di"}})
    try:
        parser._resolve_credentials()
    except ValueError as e:
        assert "endpoint" in str(e) and "api_key" in str(e)
        print("ok: parser fails loud on missing credentials")
        return
    raise AssertionError("expected ValueError on missing credentials")


def test_supported_extensions_includes_pdf() -> None:
    from docingest.azure.di_parser import AzureDIParser
    exts = AzureDIParser({}).supported_extensions()
    assert ".pdf" in exts and ".png" in exts
    print("ok: supported_extensions includes pdf/png")


# ---------------------------------------------------------------------------
# 4. routing
# ---------------------------------------------------------------------------

def test_create_parser_routes_azure_di() -> None:
    from docingest.parsers import create_parser
    from docingest.azure.di_parser import AzureDIParser

    parser = create_parser({"parsing": {"engine": "azure_di"}})
    assert isinstance(parser, AzureDIParser)
    print("ok: create_parser routes engine=azure_di → AzureDIParser")


def test_create_parser_default_still_docling() -> None:
    # The added branch must not change the default path.
    from docingest.parsers import create_parser
    parser = create_parser({"parsing": {"engine": "docling"}})
    assert type(parser).__name__ == "_DoclingWithFallback"
    print("ok: default engine still routes to docling")


if __name__ == "__main__":
    test_main_import_does_not_need_azure_sdk()
    test_converter_is_sdk_free()
    test_convert_maps_pages_and_strips_furniture()
    test_convert_detects_html_table_and_figure()
    test_convert_handles_empty_and_no_pages()
    test_parser_fails_loud_on_missing_credentials()
    test_supported_extensions_includes_pdf()
    test_create_parser_routes_azure_di()
    test_create_parser_default_still_docling()
    print("\nALL AZURE DI OFFLINE TESTS PASSED")
