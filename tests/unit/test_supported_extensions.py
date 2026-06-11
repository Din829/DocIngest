"""Supported-input-extension catalog + GUI format gate.

Covers:
  * parsers.supported_input_extensions — config-driven composition
    (parser set + legacy Office + zip, each following its switch)
  * gui_logic.split_supported — extension gate with directory pass-through
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.config import load_config
from docingest.parsers import supported_input_extensions


def test_default_catalog():
    """Defaults: parser formats + legacy Office + zip are all present."""
    print("=== Test: default catalog ===")
    config = load_config()
    exts = supported_input_extensions(config)

    # Parser-level formats (Docling + Text fallback).
    for ext in (".pdf", ".docx", ".pptx", ".xlsx", ".html", ".md", ".csv", ".png"):
        assert ext in exts, f"{ext} missing from default catalog"
    # Pipeline-level inputs, on by default.
    for ext in (".xls", ".doc", ".ppt", ".zip"):
        assert ext in exts, f"{ext} missing from default catalog"
    # Sanity: junk never appears.
    assert ".exe" not in exts
    print(f"  {len(exts)} extensions, parser + pipeline levels OK")


def test_switches_remove_extensions():
    """Turning a feature off removes its extension from the catalog."""
    print("=== Test: config switches ===")
    config = load_config(cli_overrides={
        "parsing": {
            "zip": {"enabled": False},
            "xls": {"auto_convert_to_xlsx": False},
        }
    })
    exts = supported_input_extensions(config)
    assert ".zip" not in exts, ".zip should follow parsing.zip.enabled"
    assert ".xls" not in exts, ".xls should follow auto_convert_to_xlsx"
    # Unaffected switches keep their extensions.
    assert ".doc" in exts and ".ppt" in exts and ".pdf" in exts
    print("  zip/xls removed, doc/ppt/pdf kept  OK")


def test_split_supported():
    """GUI gate: files by extension, directories pass through."""
    print("=== Test: split_supported ===")
    from docingest.gui import gui_logic

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pdf = tmp / "report.pdf"
        exe = tmp / "setup.exe"
        noext = tmp / "README"
        sub = tmp / "docs"
        for f in (pdf, exe, noext):
            f.write_bytes(b"x")
        sub.mkdir()

        result = gui_logic.split_supported(
            [str(pdf), str(exe), str(noext), str(sub)]
        )
        assert str(pdf) in result["accepted"], "supported file must pass"
        assert str(sub) in result["accepted"], "directory must pass through"
        assert str(exe) in result["rejected"], ".exe must be rejected"
        assert str(noext) in result["rejected"], "extension-less file rejected on GUI channel"
        # Case-insensitive extension match.
        upper = tmp / "SLIDES.PDF"
        upper.write_bytes(b"x")
        result2 = gui_logic.split_supported([str(upper)])
        assert str(upper) in result2["accepted"], "extension match must be case-insensitive"
    print("  accept/reject/dir/case  OK")


if __name__ == "__main__":
    test_default_catalog()
    test_switches_remove_extensions()
    test_split_supported()
    print("\nAll supported-extension tests passed.")
