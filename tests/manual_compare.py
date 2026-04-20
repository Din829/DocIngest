"""Ad-hoc comparison: PDF ground truth (pdfplumber) vs DocIngest md output.

Usage: python tests/manual_compare.py <pdf_name_without_ext>
"""
import re
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "test_docs" / "2"
MD_DIR = ROOT / "knowledge" / "test2" / "sources"


def extract_pdf_text(pdf_path: Path) -> tuple[str, list[str]]:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for pg in pdf.pages:
            pages.append((pg.extract_text() or "").strip())
    return "\n".join(pages), pages


def extract_md(md_path: Path) -> str:
    text = md_path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    return text


def find_tokens(text: str, pattern: str) -> list[str]:
    return re.findall(pattern, text)


def summarize(label: str, items: list[str]) -> None:
    uniq = sorted(set(items))
    print(f"  {label}: count={len(items)} unique={len(uniq)}")
    for t in uniq[:40]:
        print(f"    - {t}")
    if len(uniq) > 40:
        print(f"    ... (+{len(uniq)-40} more)")


def compare(stem: str) -> None:
    pdf_path = next(PDF_DIR.glob(f"{stem}*.pdf"))
    md_path = next(MD_DIR.glob(f"{stem}*.md"))
    print(f"\n=== PDF: {pdf_path.name}")
    print(f"=== MD : {md_path.name}")

    pdf_text, pdf_pages = extract_pdf_text(pdf_path)
    md_text = extract_md(md_path)

    print(f"\n[Length] pdf chars={len(pdf_text)}  md chars={len(md_text)}")
    print(f"[Pages ] pdf pages={len(pdf_pages)}  per-page lens={[len(p) for p in pdf_pages]}")

    patterns = {
        "money_円": r"[0-9,]+\s*円",
        "money_万円": r"[0-9,\.]+\s*万円",
        "percent": r"[0-9\.]+\s*[%％]",
        "reiwa": r"令和\s*[0-9元]+\s*年\s*[0-9]+\s*月(?:\s*[0-9]+\s*日)?",
        "phone": r"0\d{1,4}[-‐ー][-0-9]{4,}",
        "postal": r"〒\s*\d{3}[-‐]?\d{4}",
        "area_m2": r"[0-9\.]+\s*[㎡m][²2]?",
        "rooms": r"\d+\s*[LDKSR]+",
    }

    print("\n--- PDF tokens ---")
    pdf_tokens = {}
    for name, pat in patterns.items():
        toks = find_tokens(pdf_text, pat)
        pdf_tokens[name] = toks
        summarize(name, toks)

    print("\n--- MD tokens ---")
    md_tokens = {}
    for name, pat in patterns.items():
        toks = find_tokens(md_text, pat)
        md_tokens[name] = toks
        summarize(name, toks)

    print("\n--- Missing from MD (present in PDF but not in MD) ---")
    for name in patterns:
        p_set = set(pdf_tokens[name])
        m_set = set(md_tokens[name])
        missing = sorted(p_set - m_set)
        extra = sorted(m_set - p_set)
        if missing:
            print(f"  [{name}] MISSING {len(missing)}: {missing[:20]}")
        if extra:
            print(f"  [{name}] EXTRA   {len(extra)}: {extra[:20]}")


if __name__ == "__main__":
    stem = sys.argv[1] if len(sys.argv) > 1 else "0253324"
    compare(stem)
