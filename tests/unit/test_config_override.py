"""Test 7: Config overrides + strategy forcing + protection toggles."""
import sys
import json
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from docingest.config import load_config
from docingest.parsers import create_parser
from docingest.chunkers import create_chunker
from docingest.chunkers.recursive import RecursiveChunker
from docingest.chunkers.heading import HeadingChunker
from docingest.chunkers import AutoChunker
from docingest.pipeline import run_pipeline


def test_strategy_override():
    """Test 7a: Force different chunking strategies via config."""
    print("=== Test 7a: Strategy override ===")

    doc = (
        "# Title\n\n## Section 1\n\nContent 1.\n\n## Section 2\n\nContent 2.\n"
    )
    meta = {"source": "test.md", "format": "md"}

    # Force recursive (should NOT split by headings)
    config_r = load_config(cli_overrides={"chunking": {"strategy": "recursive"}})
    cr = create_chunker(config_r)
    assert isinstance(cr, RecursiveChunker)
    chunks_r = cr.chunk(doc, meta)
    print(f"  recursive: {len(chunks_r)} chunks, type={type(cr).__name__}")

    # Force heading
    config_h = load_config(cli_overrides={"chunking": {"strategy": "heading"}})
    ch = create_chunker(config_h)
    assert isinstance(ch, HeadingChunker)
    chunks_h = ch.chunk(doc, meta)
    print(f"  heading:   {len(chunks_h)} chunks, type={type(ch).__name__}")

    # Heading should produce more chunks (splits at ##)
    assert len(chunks_h) >= len(chunks_r), "Heading should split more than recursive for structured doc"
    print("  heading >= recursive  OK")

    # Force auto (default)
    config_a = load_config(cli_overrides={"chunking": {"strategy": "auto"}})
    ca = create_chunker(config_a)
    assert isinstance(ca, AutoChunker)
    print(f"  auto:      type={type(ca).__name__}  OK")

    print("Test 7a PASSED\n")


def test_token_size_override():
    """Test 7b: Override max_tokens and verify chunk sizes change."""
    print("=== Test 7b: Token size override ===")

    long_text = "Word " * 2000  # ~2000 tokens
    meta = {"source": "test.md", "format": "txt"}

    # Default 512
    config_512 = load_config()
    cr_512 = RecursiveChunker(config_512)
    chunks_512 = cr_512.chunk(long_text, meta)

    # Override to 256
    config_256 = load_config(cli_overrides={"chunking": {"max_tokens": 256}})
    cr_256 = RecursiveChunker(config_256)
    chunks_256 = cr_256.chunk(long_text, meta)

    # Override to 1024
    config_1024 = load_config(cli_overrides={"chunking": {"max_tokens": 1024}})
    cr_1024 = RecursiveChunker(config_1024)
    chunks_1024 = cr_1024.chunk(long_text, meta)

    print(f"  max_tokens=256:  {len(chunks_256)} chunks")
    print(f"  max_tokens=512:  {len(chunks_512)} chunks")
    print(f"  max_tokens=1024: {len(chunks_1024)} chunks")

    assert len(chunks_256) > len(chunks_512) > len(chunks_1024)
    print("  256 > 512 > 1024 chunk count  OK")
    print("Test 7b PASSED\n")


def test_format_strategy_override():
    """Test 7c: Override format_strategies mapping."""
    print("=== Test 7c: Format strategy override ===")

    config = load_config(cli_overrides={
        "chunking": {
            "auto": {
                "format_strategies": {
                    "pptx": "recursive",  # Override: PPTX uses recursive instead of slide
                    "default": "scoring",
                }
            }
        }
    })

    ac = AutoChunker(config)

    # PPTX should now use recursive (not slide)
    strategy = ac._select_strategy("slide content", {"format": "pptx"})
    assert strategy == "recursive", f"Expected recursive, got {strategy}"
    print(f"  PPTX → {strategy} (overridden from slide)  OK")

    # Other formats unchanged
    strategy2 = ac._select_strategy("data", {"format": "xlsx"})
    # xlsx not in override → uses default from base config
    print(f"  XLSX → {strategy2}")

    print("Test 7c PASSED\n")


def test_pipeline_no_chunks():
    """Test 7d: Full pipeline with chunking disabled."""
    print("=== Test 7d: Pipeline no-chunks mode ===")

    inputdir = tempfile.mkdtemp()
    outdir = tempfile.mkdtemp()

    (Path(inputdir) / "doc.md").write_text("# Test\n\nContent.\n", encoding="utf-8")

    config = load_config(cli_overrides={
        "output": {"dir": outdir},
        "chunking": {"enabled": False},
    })

    parser = create_parser(config)
    result = run_pipeline([Path(inputdir)], config, parser, chunker=None)

    assert result.successful == 1
    assert result.total_chunks == 0
    assert not (Path(outdir) / "chunks.jsonl").exists()
    assert (Path(outdir) / "index.json").exists()
    assert len(list((Path(outdir) / "sources").glob("*.md"))) == 1

    print(f"  Success: {result.successful}, Chunks: {result.total_chunks}")
    print(f"  chunks.jsonl exists: False  OK")
    print(f"  sources/*.md count: 1  OK")
    print(f"  index.json exists: True  OK")

    shutil.rmtree(inputdir)
    shutil.rmtree(outdir)
    print("Test 7d PASSED\n")


if __name__ == "__main__":
    test_strategy_override()
    test_token_size_override()
    test_format_strategy_override()
    test_pipeline_no_chunks()
    print("ALL TESTS PASSED")
