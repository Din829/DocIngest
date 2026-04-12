"""Test 4: Mixed content + Test 5: Error handling + Test 6: Output consistency."""
import sys
import json
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.config import load_config
from docingest.chunkers import AutoChunker
from docingest.parsers import create_parser
from docingest.chunkers import create_chunker
from docingest.pipeline import run_pipeline


def test_mixed_content():
    """Test 4: Mixed content document (text + table + code + list + quote)."""
    print("=== Test 4: Mixed content ===")
    config = load_config()
    ac = AutoChunker(config)

    mixed_doc = (
        "# Mixed Document\n\n"
        "## Introduction\n\n"
        "This document contains various content types.\n\n"
        "## Data Section\n\n"
        "| Metric | Q1 | Q2 |\n"
        "|--------|-----|-----|\n"
        "| Revenue | 10B | 12B |\n"
        "| Profit  | 1B  | 1.2B |\n\n"
        "The table above shows performance.\n\n"
        "## Code Example\n\n"
        "```python\n"
        "def analyze():\n"
        "    return pd.DataFrame(data)\n"
        "```\n\n"
        "## Action Items\n\n"
        "- Revenue grew consistently\n"
        "- Profit margins improved\n"
        "- Growth rate stabilized\n\n"
        "## Quote\n\n"
        "> Our performance exceeded expectations.\n"
        "> The strategic pivot proved right.\n\n"
        "## Conclusion\n\n"
        "Strong year with positive outlook.\n"
    )

    chunks = ac.chunk(mixed_doc, {"source": "mixed.md", "format": "md"})
    print(f"  Total chunks: {len(chunks)}")

    all_text = " ".join(c.text for c in chunks)

    # Table intact
    table_chunks = [c for c in chunks if "| Revenue |" in c.text]
    assert len(table_chunks) >= 1
    for tc in table_chunks:
        assert "| Profit  |" in tc.text, "Table rows split!"
    print("  Table intact  OK")

    # Code block intact
    code_chunks = [c for c in chunks if "def analyze" in c.text]
    assert len(code_chunks) >= 1
    for cc in code_chunks:
        assert "return pd.DataFrame" in cc.text, "Code split!"
    print("  Code block intact  OK")

    # List present
    assert "- Revenue grew" in all_text
    print("  List present  OK")

    # Quote present
    assert "Our performance" in all_text
    print("  Quote present  OK")

    # Title paths
    paths = [c.metadata.get("title_path", "") for c in chunks]
    assert any("Data Section" in p for p in paths)
    assert any("Conclusion" in p for p in paths)
    print("  Title paths correct  OK")
    print("Test 4 PASSED\n")


def test_error_handling():
    """Test 5: Error handling — bad files, missing files, fallback chain."""
    print("=== Test 5: Error handling ===")

    inputdir = tempfile.mkdtemp()
    outdir = tempfile.mkdtemp()

    # Good file
    (Path(inputdir) / "good.md").write_text("# Good\n\nContent.", encoding="utf-8")

    # Binary file (will fail Docling, fallback should catch)
    (Path(inputdir) / "bad.bin").write_bytes(b"\x00\x01\x02\x03\x00\xff" * 100)

    # Empty file
    (Path(inputdir) / "empty.txt").write_text("", encoding="utf-8")

    config = load_config()
    config["output"]["dir"] = outdir

    parser = create_parser(config)
    chunker = create_chunker(config)

    result = run_pipeline([Path(inputdir)], config, parser, chunker)

    print(f"  Total: {result.total_files}, Success: {result.successful}, Failed: {result.failed}")
    # good.md should succeed, bad.bin should fail, empty.txt depends
    assert result.successful >= 1, "At least good.md should succeed"
    print(f"  Errors: {[e['file'] for e in result.errors]}")

    # Errors should be in errors.json
    errors_path = Path(outdir) / "errors.json"
    if result.errors:
        assert errors_path.exists(), "errors.json should exist"
        errors_data = json.loads(errors_path.read_text(encoding="utf-8"))
        assert len(errors_data) == result.failed
        print(f"  errors.json: {len(errors_data)} entries  OK")

    shutil.rmtree(inputdir)
    shutil.rmtree(outdir)
    print("Test 5 PASSED\n")


def test_output_consistency():
    """Test 6: Output consistency — sources + chunks + index all match."""
    print("=== Test 6: Output consistency ===")

    inputdir = tempfile.mkdtemp()
    outdir = tempfile.mkdtemp()

    # Create 3 test documents
    (Path(inputdir) / "doc1.md").write_text(
        "# Doc1\n\n## A\n\nContent A.\n\n## B\n\nContent B.\n", encoding="utf-8"
    )
    (Path(inputdir) / "doc2.md").write_text(
        "# Doc2\n\nPlain text " * 50 + "\n", encoding="utf-8"
    )
    (Path(inputdir) / "doc3.md").write_text(
        "# Doc3\n\n## X\n\n" + "Paragraph. " * 200 + "\n\n## Y\n\nShort.\n",
        encoding="utf-8",
    )

    config = load_config()
    config["output"]["dir"] = outdir

    parser = create_parser(config)
    chunker = create_chunker(config)

    result = run_pipeline([Path(inputdir)], config, parser, chunker)

    # All should succeed
    assert result.successful == 3 and result.failed == 0

    # Check sources
    sources = sorted((Path(outdir) / "sources").glob("*.md"))
    assert len(sources) == 3
    print(f"  Sources: {len(sources)}  OK")

    # Check index.json
    index = json.loads((Path(outdir) / "index.json").read_text(encoding="utf-8"))
    assert index["stats"]["total_files"] == 3
    assert index["stats"]["total_chunks"] == result.total_chunks
    print(f"  index.json: {index['stats']['total_files']} files, {index['stats']['total_chunks']} chunks  OK")

    # Check chunks.jsonl
    chunks_lines = (Path(outdir) / "chunks.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(chunks_lines) == result.total_chunks
    print(f"  chunks.jsonl: {len(chunks_lines)} records  OK")

    # Verify chunk count consistency
    assert (
        index["stats"]["total_chunks"] == len(chunks_lines) == result.total_chunks
    ), "Chunk count mismatch between index, jsonl, and pipeline result!"
    print(f"  Consistency: index={index['stats']['total_chunks']}, jsonl={len(chunks_lines)}, pipeline={result.total_chunks}  OK")

    # Verify all chunks have path injection
    injected = 0
    for line in chunks_lines:
        c = json.loads(line)
        if c["text"].startswith("[来源:"):
            injected += 1
    print(f"  Path injection: {injected}/{len(chunks_lines)} chunks")
    assert injected == len(chunks_lines), "Not all chunks have path injection!"
    print("  All chunks injected  OK")

    # Verify chunk IDs are unique
    ids = [json.loads(l)["id"] for l in chunks_lines]
    assert len(ids) == len(set(ids)), "Duplicate chunk IDs!"
    print("  Unique IDs  OK")

    shutil.rmtree(inputdir)
    shutil.rmtree(outdir)
    print("Test 6 PASSED\n")


if __name__ == "__main__":
    test_mixed_content()
    test_error_handling()
    test_output_consistency()
    print("ALL TESTS PASSED")
