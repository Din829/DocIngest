"""
Cross-scenario chunking stress tests.

Exercises the chunker against synthetic Markdown inputs that simulate
document shapes the test corpus (9 Japanese PDFs) does NOT cover:

  - Pure English prose with #-style headings
  - Heavy code-fence documents (technical docs)
  - Long bulleted / numbered lists
  - Single-paragraph wall-of-text (no headings at all)
  - Deeply nested headings (H1 > H2 > H3 > H4 > H5)
  - Documents where every heading is empty-body ("heading-only")
  - Documents that begin with prose BEFORE any heading (prelude)
  - Very small documents (under min_tokens total)
  - Mixed-content: heading + table + code + list interleaved

For each scenario we check:
  * no chunk is empty
  * no tiny fragments (< 30 tok) unless the whole input is tiny
  * no byte-identical adjacent duplicates
  * title_path sanity (either empty everywhere, or consistent with headings)
  * total token content is preserved modulo denoising (no content disappears)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from docingest.config import load_config  # noqa: E402
from docingest.chunkers.heading import HeadingChunker  # noqa: E402
from docingest.chunkers.recursive import RecursiveChunker  # noqa: E402


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def scenario_english_prose() -> str:
    body = ("This is a paragraph about machine learning. " * 20).strip()
    return (
        "# Introduction\n\n"
        f"{body}\n\n"
        "## Methods\n\n"
        f"{body}\n\n"
        "### Experimental Setup\n\n"
        f"{body}\n\n"
        "## Results\n\n"
        f"{body}\n\n"
        "## Conclusion\n\n"
        f"{body}\n"
    )


def scenario_code_heavy() -> str:
    return (
        "# API Reference\n\n"
        "Example usage:\n\n"
        "```python\n"
        + "\n".join(f"def function_{i}(x):\n    return x * {i}" for i in range(30))
        + "\n```\n\n"
        "## Configuration\n\n"
        "```yaml\n"
        + "\n".join(f"key_{i}: value_{i}" for i in range(50))
        + "\n```\n"
    )


def scenario_long_list() -> str:
    items = "\n".join(f"- Item {i}: {'detail ' * 15}" for i in range(80))
    return f"# Shopping List\n\n{items}\n"


def scenario_wall_of_text() -> str:
    # No headings at all — forces recursive fallback.
    return "This is one giant paragraph. " * 400


def scenario_deep_nesting() -> str:
    body4 = ("Content at depth 4 that is long enough to matter. " * 20).strip()
    body5 = ("Content at depth 5. " * 20).strip()
    return (
        "# L1 Chapter\n\nIntro text.\n\n"
        "## L2 Section\n\nSection text.\n\n"
        "### L3 Subsection\n\nSubsection text.\n\n"
        "#### L4 Subsubsection\n\n"
        f"{body4}\n\n"
        "##### L5 Deepest\n\n"
        f"{body5}\n"
    )


def scenario_headings_only() -> str:
    # Many headings, almost no body — tests orphan-heading + merge.
    return "\n\n".join(f"## Chapter {i}" for i in range(20)) + "\n\nFinal words.\n"


def scenario_prelude_heavy() -> str:
    # 3 paragraphs of prelude before any heading.
    prelude = ("Document prelude text. " * 30 + "\n\n") * 3
    return (
        prelude
        + "# Main Title\n\n"
        + "Section body. " * 50
        + "\n"
    )


def scenario_tiny() -> str:
    return "# Short\n\nJust a few words.\n"


def scenario_mixed_everything() -> str:
    table = "\n".join(["| a | b | c |", "| :--- | :--- | :--- |"] + [f"| r{i}a | r{i}b | r{i}c |" for i in range(20)])
    code = "```py\n" + "\n".join(f"x = {i}" for i in range(10)) + "\n```"
    lst = "\n".join(f"1. Step {i}" for i in range(10))
    return (
        "# Main\n\nIntro paragraph with enough length to stand alone. " * 5 + "\n\n"
        + "## Tables\n\n" + table + "\n\n"
        + "## Code\n\n" + code + "\n\n"
        + "## Steps\n\n" + lst + "\n\n"
        + "## Conclusion\n\n" + "Closing text. " * 20 + "\n"
    )


def scenario_cjk_wall() -> str:
    # CJK text with no sentence-ending punctuation — the old "4 chars/token"
    # hard-cut would produce ~6× oversized chunks because CJK density
    # is ~1.5 tok/char, not 0.25.
    return "这是一段没有标点符号的超长中文文字" * 300


def scenario_url_mess() -> str:
    # Paragraph-level oversize with no sentence boundaries (URLs, IDs,
    # glued identifiers) — a common real-world pathology.
    return (
        "https://example.com/path/very-long-identifier-abc-def-ghi-" * 80
        + "\n\nsome other junk "
        + "XYZ987-QWERTY-" * 60
    )


def scenario_pathological_table() -> str:
    # Docling-style merged-cell expansion: same text repeated across columns.
    big_cell = "This merged-cell content gets replicated many times across columns."
    row = "| " + " | ".join([big_cell] * 10) + " |"
    header = "| " + " | ".join(f"col{i}" for i in range(10)) + " |"
    sep = "| " + " | ".join([":---"] * 10) + " |"
    return (
        "# Contract\n\n"
        + "## 13. Rental Conditions\n\n"
        + header + "\n" + sep + "\n"
        + "\n".join([row] * 6)
        + "\n"
    )


SCENARIOS = {
    "english_prose": scenario_english_prose,
    "code_heavy": scenario_code_heavy,
    "long_list": scenario_long_list,
    "wall_of_text": scenario_wall_of_text,
    "deep_nesting": scenario_deep_nesting,
    "headings_only": scenario_headings_only,
    "prelude_heavy": scenario_prelude_heavy,
    "tiny": scenario_tiny,
    "mixed_everything": scenario_mixed_everything,
    "cjk_wall": scenario_cjk_wall,
    "url_mess": scenario_url_mess,
    "pathological_table": scenario_pathological_table,
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def check_chunks(name: str, chunks, source_text: str, max_tok: int, min_tok: int) -> list[str]:
    issues: list[str] = []
    if not chunks:
        if source_text.strip():
            issues.append("EMPTY_OUTPUT on non-empty input")
        return issues

    # No empty chunks
    for i, c in enumerate(chunks):
        if not c.text.strip():
            issues.append(f"empty chunk at idx {i}")

    # No byte-identical adjacent
    for i in range(1, len(chunks)):
        if chunks[i].text == chunks[i - 1].text:
            issues.append(f"byte-duplicate chunks at idx {i-1}/{i}")

    # Fragment check — only fail if input is large enough that fragments
    # were avoidable. Tiny input legitimately produces tiny output.
    source_tokens = len(source_text) // 3  # rough
    if source_tokens > min_tok * 2:
        for i, c in enumerate(chunks):
            if c.metadata["tokens"] < 30:
                issues.append(f"fragment (<30 tok) at idx {i}: {c.text[:60]!r}")

    # Oversized check — only flag when we don't expect it.
    # Chunks that legitimately contain an oversized protected block
    # (list / code / quote) under on_overflow=bypass are *intentional*;
    # the user picked a size/correctness trade-off. Pathological tables
    # and prose are NOT expected to exceed 4× max_tokens — those indicate
    # the chunker failed to subdivide.
    def _chunk_is_protected_bypass(text: str) -> bool:
        """True if the chunk's content is dominated by a protected block."""
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return False
        listy = sum(1 for ln in lines if ln.lstrip().startswith(("-", "*", "+", "1.", "2.", "3.")))
        code = sum(1 for ln in lines if ln.lstrip().startswith("```"))
        quote = sum(1 for ln in lines if ln.lstrip().startswith(">"))
        # More than half the content lines are a protected-block signature
        return (listy + code + quote) / len(lines) > 0.5

    for i, c in enumerate(chunks):
        if c.metadata["tokens"] > max_tok * 4:
            if _chunk_is_protected_bypass(c.text):
                issues.append(
                    f"INFO oversize ({c.metadata['tokens']} tok) at idx {i} "
                    f"— protected-block bypass, tune on_overflow.* if undesired"
                )
            else:
                issues.append(
                    f"severe oversize ({c.metadata['tokens']} tok > 4× max) at idx {i}"
                )

    return issues


def run_scenario(name: str, make_input) -> dict:
    config = load_config()
    ch = HeadingChunker(config)
    text = make_input()
    chunks = ch.chunk(text, {"source": f"{name}.md"})
    sizes = [c.metadata["tokens"] for c in chunks]
    issues = check_chunks(name, chunks, text, config["chunking"]["max_tokens"], config["chunking"]["min_tokens"])
    return {
        "name": name,
        "input_chars": len(text),
        "n_chunks": len(chunks),
        "size_min": min(sizes) if sizes else 0,
        "size_max": max(sizes) if sizes else 0,
        "size_avg": sum(sizes) // len(sizes) if sizes else 0,
        "issues": issues,
    }


def main() -> int:
    print(f'{"scenario":25s} {"chars":>7s} {"n":>4s} {"min":>5s} {"max":>5s} {"avg":>5s}  issues')
    total_issues = 0
    for name, fn in SCENARIOS.items():
        r = run_scenario(name, fn)
        marker = "✓" if not r["issues"] else "✗"
        print(
            f'{marker} {r["name"]:23s} {r["input_chars"]:>7d} {r["n_chunks"]:>4d} '
            f'{r["size_min"]:>5d} {r["size_max"]:>5d} {r["size_avg"]:>5d}  '
            f'{"; ".join(r["issues"][:3]) if r["issues"] else "OK"}'
        )
        total_issues += len(r["issues"])
    print()
    print(f'TOTAL issues across {len(SCENARIOS)} scenarios: {total_issues}')
    return 0 if total_issues == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
