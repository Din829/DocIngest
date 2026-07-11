"""
Unit tests for three xlsx fidelity fixes (2026-07-11):

  A. Hyperlink targets: cell.value only carries display text; the URL lives
     on cell.hyperlink and was dropped entirely (measured 101/101 external
     targets lost on a real WBS workbook). Now rendered as [text](url).
  B. Cell comments: filling guidance often lives ONLY in comments (measured
     27/28 lost on the same workbook; invisible on page renders, so Vision
     can never recover them). Now rendered as 〔注: …〕.
  C. Page→section attribution for Vision supplements: sections are per-SHEET
     but LibreOffice renders long sheets to many pages, so the legacy
     sections[idx] alignment misattributed continuation pages (observed:
     a 案件情報 page's supplement under the last WBS sheet's heading).
     _xlsx_page_owner_index owns the anchored-range rule.
  D. URL echo-hallucination guard: Vision repetition-loops on long
     small-type URLs (measured: 293-char truth → 978-char echo). Echoed
     variants are cut back to the ground-truth URL; legitimate sub-paths
     survive.

Pure in-memory openpyxl workbooks + string transforms — no parsing, no
network. Fast and offline.

Run:
    python tests/unit/test_xlsx_links_comments_attribution.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

import openpyxl
from openpyxl.comments import Comment

from docingest.parsers.docling_parser import (
    _collect_xlsx_image_anchors,
    _render_xlsx_sheet_to_markdown,
)
from docingest.pipeline import _truncate_url_echoes, _xlsx_page_owner_index


def _render(ws) -> str:
    return "\n".join(_render_xlsx_sheet_to_markdown(ws))


# ---------------------------------------------------------------------------
# A. Hyperlinks
# ---------------------------------------------------------------------------

def test_external_hyperlink_rendered():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "header"
    ws["A2"] = "Playbook 参照"
    ws["A2"].hyperlink = "https://example.com/playbook/page1"
    md = _render(ws)
    assert "[Playbook 参照](https://example.com/playbook/page1)" in md, md
    print("  ✓ external hyperlink -> [text](url)")


def test_internal_anchor_not_wrapped():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "header"
    ws["A2"] = "リストに戻る"
    # Internal anchor: location without target (openpyxl models sheet-local
    # links as Hyperlink(ref=..., location="Sheet!A1"), target=None).
    from openpyxl.worksheet.hyperlink import Hyperlink
    ws["A2"].hyperlink = Hyperlink(ref="A2", location="'PBIリスト'!A1")
    md = _render(ws)
    assert "リストに戻る" in md
    assert "(" not in md.split("リストに戻る")[1].split("|")[0], md
    print("  ✓ internal anchor stays plain text")


def test_self_url_not_double_wrapped():
    wb = openpyxl.Workbook()
    ws = wb.active
    url = "https://example.com/finance-webchat/"
    ws["A1"] = url
    ws["A1"].hyperlink = url
    md = _render(ws)
    assert md.count(url) == 1, md
    assert f"[{url}]" not in md, md
    print("  ✓ cell text already containing the URL is not re-wrapped")


def test_hyperlink_special_chars_escaped():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "link"
    ws["A1"].hyperlink = "https://example.com/a(b)|c d"
    md = _render(ws)
    assert "(https://example.com/a%28b%29%7Cc%20d)" in md, md
    print("  ✓ ()| and spaces in URL are %-escaped")


# ---------------------------------------------------------------------------
# B. Comments
# ---------------------------------------------------------------------------

def test_comment_rendered_inline():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "開始日"
    ws["A1"].comment = Comment("yyyy/mm/dd形式で入力してください。", "author")
    md = _render(ws)
    assert "開始日 〔注: yyyy/mm/dd形式で入力してください。〕" in md, md
    print("  ✓ comment -> inline 〔注: …〕")


def test_comment_on_empty_cell_preserved():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "header"
    ws["B2"].comment = Comment("空セルにも注釈が付くことがある", "author")
    md = _render(ws)
    assert "〔注: 空セルにも注釈が付くことがある〕" in md, md
    print("  ✓ comment on an empty cell is not lost")


def test_comment_newlines_and_pipes_sanitised():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "cell"
    ws["A1"].comment = Comment("line1\nline2 | pipe", "author")
    md = _render(ws)
    assert "line1 line2 \\| pipe" in md, md
    print("  ✓ comment newlines/pipes sanitised for table context")


# ---------------------------------------------------------------------------
# B2. Drawing-layer shape labels (text boxes / callouts)
# ---------------------------------------------------------------------------

def _minimal_xlsx_with_shapes(path):
    """Hand-built minimal xlsx: one sheet, one drawing with a labelled
    shape, a group holding a nested labelled shape, a text-less connector,
    and a two-paragraph label. openpyxl cannot AUTHOR shapes, so the zip
    is assembled from raw OOXML parts (all namespaces as Excel writes them).
    """
    import zipfile
    XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

    def sp(text_paras):
        body = "".join(
            f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in text_paras
        )
        return (f'<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="1" name="s"/>'
                f"<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr/>"
                f"<xdr:txBody><a:bodyPr/>{body}</xdr:txBody></xdr:sp>")

    def anchor(row, col, inner):
        return (f"<xdr:twoCellAnchor><xdr:from><xdr:col>{col}</xdr:col>"
                f"<xdr:colOff>0</xdr:colOff><xdr:row>{row}</xdr:row>"
                f"<xdr:rowOff>0</xdr:rowOff></xdr:from>"
                f"<xdr:to><xdr:col>{col+1}</xdr:col><xdr:colOff>0</xdr:colOff>"
                f"<xdr:row>{row+1}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
                f"{inner}<xdr:clientData/></xdr:twoCellAnchor>")

    drawing = (
        f'<xdr:wsDr xmlns:xdr="{XDR}" xmlns:a="{A}">'
        + anchor(4, 1, sp(["2-1"]))                              # plain label
        + anchor(9, 2, f"<xdr:grpSp><xdr:nvGrpSpPr>"
                       f'<xdr:cNvPr id="9" name="g"/><xdr:cNvGrpSpPr/>'
                       f"</xdr:nvGrpSpPr><xdr:grpSpPr/>"
                       f"{sp(['S-020 企業詳細画面'])}</xdr:grpSp>")  # nested in group
        + anchor(14, 0, "<xdr:cxnSp><xdr:nvCxnSpPr>"
                        '<xdr:cNvPr id="5" name="c"/><xdr:cNvCxnSpPr/>'
                        "</xdr:nvCxnSpPr><xdr:spPr/></xdr:cxnSp>")   # connector: skip
        + anchor(19, 3, sp(["line one", "line two"]))            # multi-paragraph
        + "</xdr:wsDr>"
    )
    parts = {
        "[Content_Types].xml":
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            "</Types>",
        "_rels/.rels":
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml":
            f'<?xml version="1.0"?><workbook xmlns="{M}" xmlns:r="{R}">'
            '<sheets><sheet name="レイアウト" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml":
            f'<?xml version="1.0"?><worksheet xmlns="{M}" xmlns:r="{R}">'
            '<sheetData/><drawing r:id="rId1"/></worksheet>',
        "xl/worksheets/_rels/sheet1.xml.rels":
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{R}/drawing" Target="../drawings/drawing1.xml"/></Relationships>',
        "xl/drawings/drawing1.xml": drawing,
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, content in parts.items():
            z.writestr(name, content)


def test_shape_texts_collected_from_drawing_xml():
    import tempfile, os
    fd, p = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        _minimal_xlsx_with_shapes(p)
        anchors = _collect_xlsx_image_anchors(Path(p))
        entries = anchors.get("レイアウト", [])
        texts = {e["text"]: (e["row"], e["col"]) for e in entries if "text" in e}
        assert texts.get("2-1") == (5, 2), entries              # 0-based 4,1 -> 1-based
        assert texts.get("S-020 企業詳細画面") == (10, 3), entries  # nested in grpSp
        assert texts.get("line one line two") == (20, 4), entries  # paragraphs joined
        assert len(texts) == 3, entries                          # connector skipped
    finally:
        os.unlink(p)
    print("  ✓ shape labels harvested from drawing XML (incl. grouped; connector skipped)")


def test_shape_labels_rendered_inline():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "header"
    ws["A3"] = "cell on shape row"
    md = "\n".join(_render_xlsx_sheet_to_markdown(
        ws, shape_texts={3: ["2-1"], 7: ["S-020 企業詳細画面"]}
    ))
    assert "〔図形: 2-1〕" in md, md
    # Row 7 has no cell content at all — the label must still appear.
    assert "〔図形: S-020 企業詳細画面〕" in md, md
    print("  ✓ shape labels rendered, including on cell-empty rows")


# ---------------------------------------------------------------------------
# C. Page -> section attribution
# ---------------------------------------------------------------------------

def test_owner_index_full_map():
    visible = ["Read me", "案件情報", "WBS"]
    page_map = {"Read me": 1, "案件情報": 7, "WBS": 8}
    total = 56
    assert _xlsx_page_owner_index(visible, page_map, 1, total) == 0
    assert _xlsx_page_owner_index(visible, page_map, 6, total) == 0
    assert _xlsx_page_owner_index(visible, page_map, 7, total) == 1
    assert _xlsx_page_owner_index(visible, page_map, 8, total) == 2
    assert _xlsx_page_owner_index(visible, page_map, 56, total) == 2
    print("  ✓ full map: continuation pages attributed to owning sheet")


def test_owner_index_conservative_fallbacks():
    visible = ["A", "B", "C"]
    total = 10
    # Unmapped successor: B's range end is unanchored -> A pages that fall
    # in [1, C.first-1] can't be trusted either -> None for range owner A.
    page_map = {"A": 1, "C": 8}
    assert _xlsx_page_owner_index(visible, page_map, 3, total) is None
    # C itself is fine (last mapped, no successor).
    assert _xlsx_page_owner_index(visible, page_map, 9, total) == 2
    # Non-increasing anchors -> disarm entirely.
    bad = {"A": 5, "B": 2, "C": 8}
    assert _xlsx_page_owner_index(visible, bad, 6, total) is None
    # Page outside any range (page 0) / empty map.
    assert _xlsx_page_owner_index(visible, {"A": 2}, 1, total) is None
    assert _xlsx_page_owner_index(visible, {}, 1, total) is None
    print("  ✓ any doubt -> None (unmapped successor / bad anchors / no map)")


# ---------------------------------------------------------------------------
# D. URL echo guard
# ---------------------------------------------------------------------------

_TRUTH = (
    "https://sites.google.com/pwc.com/japanbrandcommunications/"
    "%E3%82%AC%E3%82%A4%E3%83%89%E3%83%A9%E3%82%A4%E3%83%B3/"
    "%E3%83%86%E3%82%AF%E3%83%8E%E3%83%AD%E3%82%B8%E3%83%BC%E3%82%A2"
    "%E3%82%BB%E3%83%83%E3%83%88%E3%83%87%E3%82%B6%E3%82%A4%E3%83%B3"
    "%E3%82%AC%E3%82%A4%E3%83%89%E3%83%A9%E3%82%A4%E3%83%B3"
)


def test_echo_truncated_to_truth():
    body = f"| guideline | {_TRUTH} |"
    # Simulate the observed repetition loop: truth + fragments of itself.
    echo = _TRUTH + (
        "%E3%82%AC%E3%82%A4%E3%83%89%E3%83%A9%E3%82%A4%E3%83%B3" * 5
    )
    supplement = f"**Guideline** : {echo}"
    fixed = _truncate_url_echoes(supplement, body)
    assert _TRUTH in fixed, fixed
    assert echo not in fixed, "echoed URL should have been truncated"
    assert fixed.count("%E3%82%AC%E3%82%A4%E3%83%89") == _TRUTH.count(
        "%E3%82%AC%E3%82%A4%E3%83%89"
    ), fixed
    print("  ✓ repetition-loop URL cut back to ground truth")


def test_legit_subpath_survives():
    base = "https://sites.google.com/pwc.com/japanbrandcommunications/portal"
    body = f"see {base} for details"
    sub = base + "/visual-identity/logo-usage-v2"
    supplement = f"logo rules: {sub}"
    assert _truncate_url_echoes(supplement, body) == supplement
    print("  ✓ legitimate sub-path URL untouched")


def test_short_urls_untouched():
    body = "https://a.example.com/x plus https://b.example.com/y"
    supplement = "https://a.example.com/x/anything/else/entirely/here"
    assert _truncate_url_echoes(supplement, body) == supplement
    print("  ✓ short body URLs (<40 chars) never trigger truncation")


def test_legit_repeated_fragment_url_survives():
    # A URL whose path LEGITIMATELY repeats a segment (guideline/…guideline
    # exists in the wild — the measured truth URL itself contains the same
    # encoded word twice). Body holds a truncated prefix; Vision reads the
    # full URL. The window test alone would false-positive here (+1 repeat,
    # windows all recombine known material) — the count-explosion test
    # (needs +3) must keep it alive.
    seg = "%E3%82%AC%E3%82%A4%E3%83%89%E3%83%A9%E3%82%A4%E3%83%B3"  # ガイドライン
    body_prefix = f"https://sites.google.com/x/{seg}/{seg}"  # ≥40 chars, ends with seg
    full = body_prefix + f"/{seg}"  # legit: one more occurrence (+1), ext ≥ 24
    supplement = f"see {full}"
    assert _truncate_url_echoes(supplement, f"link: {body_prefix}") == supplement
    print("  ✓ legit URL with repeated path segment (+1) survives")


def test_explosion_threshold_cuts_at_plus_three():
    seg = "%E3%82%AC%E3%82%A4%E3%83%89%E3%83%A9%E3%82%A4%E3%83%B3"
    body_url = f"https://sites.google.com/x/{seg}/page"
    echoed = body_url + seg * 3  # +3 occurrences of a known fragment
    fixed = _truncate_url_echoes(f"see {echoed}", f"link: {body_url}")
    assert body_url in fixed and echoed not in fixed, fixed
    print("  ✓ fragment count +3 (echo fingerprint) is cut")


if __name__ == "__main__":
    for fn in [
        test_external_hyperlink_rendered,
        test_internal_anchor_not_wrapped,
        test_self_url_not_double_wrapped,
        test_hyperlink_special_chars_escaped,
        test_comment_rendered_inline,
        test_comment_on_empty_cell_preserved,
        test_comment_newlines_and_pipes_sanitised,
        test_shape_texts_collected_from_drawing_xml,
        test_shape_labels_rendered_inline,
        test_owner_index_full_map,
        test_owner_index_conservative_fallbacks,
        test_echo_truncated_to_truth,
        test_legit_subpath_survives,
        test_short_urls_untouched,
        test_legit_repeated_fragment_url_survives,
        test_explosion_threshold_cuts_at_plus_three,
    ]:
        fn()
    print("\nALL xlsx links/comments/attribution TESTS PASSED")
