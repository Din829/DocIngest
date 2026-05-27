"""
XLSX AutoShape connector direct-read hook.

Why this hook exists
--------------------
Excel files used for 画面遷移図 / フロー図 / システム構成図 routinely contain
AutoShape rectangles connected by arrow lines (xdr:cxnSp connectors). When
DocIngest renders the sheet via LibreOffice → PDF → screenshot for Vision,
LibreOffice frequently DROPS the arrow's terminal point — a connector that
visually loops from box A back to a distant box C ends up rendered as a line
fragment that "trails off" mid-canvas. Vision then describes the diagram as
a linear chain (A → B → C) because that's what it sees on the rendered
image, missing the real branch (A → B AND A → C).

The connector relationships are 100% present in the xlsx OOXML, just not
in the rendered image. By reading them directly and feeding them to Vision
as `structured_data` (ground truth), Vision can describe arrow LABELS and
visual annotations while trusting the structured block for connectivity.

What we do
----------
  * Open the xlsx zip and parse every xl/drawings/drawingN.xml.
  * Map drawingN.xml → sheet index via xl/worksheets/_rels/sheetN.xml.rels.
  * For each drawing collect:
      - boxes (xdr:sp with text content) and their EMU rectangles
      - connectors (xdr:cxnSp) and their start / end coordinates
  * Reconstruct connection relationships geometrically: each connector
    endpoint is matched to the nearest box (point-to-rectangle distance).
    LibreOffice's .xls → .xlsx conversion strips the original
    `<a:stCxn id="..."/>` references so geometry is the only path; it's
    accurate because connector endpoints in Excel snap to box edges.
  * Format the relationships as a Markdown table and write to
    parse_result.metadata["structured_extractions_per_page"][page_no].

What we DO NOT do
-----------------
  * Do NOT inject into parse_result.markdown. Connector relationships are
    diagram-internal structure; the final md should contain Vision's
    enriched description (which now becomes accurate thanks to ground
    truth), not a raw connection list.
  * Do NOT suppress Vision. Vision still runs and supplies the visual
    layer Vision is good at (arrow labels, annotations, phase grouping).
  * Do NOT raise on damaged or atypical xlsx. Any failure → HookNoOp
    (silent skip; no lineage record).

Limitations
-----------
  * sheet → page mapping: the hook currently assumes 1 sheet → 1 page,
    which is true for most画面遷移図 sheets. Sheets that LibreOffice
    renders to multiple PDF pages will get the connector data attached
    to the FIRST corresponding page only. Better page mapping would
    need LibreOffice to emit a sheet→page table.

Config:
  parsing.xlsx.extract_connector_data — master toggle (default true)
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ..config import get_nested
from ..parsers.base import ParseResult
from . import HookNoOp

logger = logging.getLogger(__name__)


# OOXML namespaces
_NS = {
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

# Endpoint-to-box-edge distance threshold (EMU; 914400 EMU = 1 inch).
# 200000 EMU ≈ 5.5 mm — generous enough to absorb LibreOffice's geometric
# rounding when converting .xls connectors, tight enough to reject
# coincidental nearness on dense pages.
_ATTACH_THRESHOLD_EMU = 200000


def _parse_drawing(xml_text: str) -> tuple[list[dict], list[dict]]:
    """
    Parse one drawingN.xml into (boxes, connectors).

    Each box: {name, text, x, y, w, h}
    Each connector: {name, sx, sy, ex, ey}  (start/end coordinates in EMU)
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], []

    boxes: list[dict] = []
    connectors: list[dict] = []

    for anchor in root.findall("xdr:twoCellAnchor", _NS):
        # Box-like shape (rectangle with text)
        sp = anchor.find("xdr:sp", _NS)
        if sp is not None:
            xfrm = sp.find(".//a:xfrm", _NS)
            if xfrm is None:
                continue
            off = xfrm.find("a:off", _NS)
            ext = xfrm.find("a:ext", _NS)
            if off is None or ext is None:
                continue
            try:
                x = int(off.get("x") or 0)
                y = int(off.get("y") or 0)
                w = int(ext.get("cx") or 0)
                h = int(ext.get("cy") or 0)
            except (TypeError, ValueError):
                continue

            # Collect text from all <a:t> nodes (multi-run shapes).
            texts = [t.text or "" for t in sp.findall(".//a:t", _NS)]
            text = "".join(texts).strip()

            name_node = sp.find(".//xdr:cNvPr", _NS)
            name = (name_node.get("name") if name_node is not None else "") or ""
            boxes.append({"name": name, "text": text, "x": x, "y": y, "w": w, "h": h})
            continue

        # Connector (xdr:cxnSp)
        cxn = anchor.find("xdr:cxnSp", _NS)
        if cxn is not None:
            xfrm = cxn.find(".//a:xfrm", _NS)
            if xfrm is None:
                continue
            off = xfrm.find("a:off", _NS)
            ext = xfrm.find("a:ext", _NS)
            if off is None or ext is None:
                continue
            try:
                x = int(off.get("x") or 0)
                y = int(off.get("y") or 0)
                w = int(ext.get("cx") or 0)
                h = int(ext.get("cy") or 0)
            except (TypeError, ValueError):
                continue
            sx, sy = x, y
            ex, ey = x + w, y + h
            # flipH / flipV swap endpoints
            if xfrm.get("flipH") == "1":
                sx, ex = ex, sx
            if xfrm.get("flipV") == "1":
                sy, ey = ey, sy

            name_node = cxn.find(".//xdr:cNvPr", _NS)
            name = (name_node.get("name") if name_node is not None else "") or ""
            connectors.append(
                {"name": name, "sx": sx, "sy": sy, "ex": ex, "ey": ey}
            )

    return boxes, connectors


def _dist_point_to_rect(px: int, py: int, box: dict) -> float:
    """Distance from point (px, py) to box rectangle edge (0 if inside)."""
    dx = max(box["x"] - px, 0, px - (box["x"] + box["w"]))
    dy = max(box["y"] - py, 0, py - (box["y"] + box["h"]))
    return (dx * dx + dy * dy) ** 0.5


def _reconstruct_edges(boxes: list[dict], connectors: list[dict]) -> list[dict]:
    """
    For each connector, attach its start / end to the nearest text-bearing box.
    Skip connectors whose endpoints don't snap to any box within threshold,
    and self-loops (start == end). Returns a list of edges:
      {source, target, connector_name, start_dist, end_dist}
    """
    text_boxes = [b for b in boxes if b["text"]]
    if not text_boxes:
        return []

    edges: list[dict] = []
    for c in connectors:
        start_box = min(text_boxes, key=lambda b: _dist_point_to_rect(c["sx"], c["sy"], b))
        end_box = min(text_boxes, key=lambda b: _dist_point_to_rect(c["ex"], c["ey"], b))
        start_dist = _dist_point_to_rect(c["sx"], c["sy"], start_box)
        end_dist = _dist_point_to_rect(c["ex"], c["ey"], end_box)

        # Both endpoints must attach within threshold (else: stray arrow,
        # decorative line, or geometry that's too noisy to trust).
        if start_dist > _ATTACH_THRESHOLD_EMU or end_dist > _ATTACH_THRESHOLD_EMU:
            continue
        # Skip self-loops and empty-text degenerate matches.
        if not start_box["text"] or not end_box["text"]:
            continue
        if start_box["text"] == end_box["text"]:
            continue

        edges.append({
            "source": start_box["text"],
            "target": end_box["text"],
            "connector_name": c["name"],
            "start_dist": start_dist,
            "end_dist": end_dist,
        })
    return edges


def _format_edges_as_markdown(edges: list[dict]) -> str:
    """
    Render the edge list as a Markdown table that fits Vision's
    GROUND TRUTH structured-data slot. Connector type comes from the
    OOXML shape name (e.g. "直線矢印コネクタ 51" / "カギ線コネクタ 82"),
    which preserves whether the arrow is straight or routed —
    useful semantic info Vision can't reliably see when the rendered
    arrow is truncated.
    """
    if not edges:
        return ""

    lines = [
        "### Diagram connector relationships",
        "",
        "Extracted directly from the source xlsx file's drawing XML "
        "(authoritative — endpoint coordinates were verified against box "
        "rectangles). Use this list when describing arrow flow direction; "
        "focus your visual description on arrow LABELS, annotations, "
        "groupings, and any nuance the list cannot convey.",
        "",
        "| Source | → | Target | Connector |",
        "| :--- | :---: | :--- | :--- |",
    ]
    for e in edges:
        lines.append(
            f"| {e['source']} | → | {e['target']} | {e['connector_name']} |"
        )
    return "\n".join(lines)


def _read_drawing_sheet_map(zf: zipfile.ZipFile) -> dict[str, int]:
    """
    Map drawing file basename (e.g. "drawing1.xml") → 1-based sheet index.

    Walks xl/worksheets/_rels/sheet{N}.xml.rels to find which drawing
    target each sheet uses. Sheets without an embedded drawing are absent
    from the map.
    """
    mapping: dict[str, int] = {}
    rel_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    for name in zf.namelist():
        # xl/worksheets/_rels/sheet1.xml.rels
        if not (name.startswith("xl/worksheets/_rels/sheet") and name.endswith(".xml.rels")):
            continue
        try:
            sheet_no = int(name.removeprefix("xl/worksheets/_rels/sheet").removesuffix(".xml.rels"))
        except ValueError:
            continue
        try:
            rels_root = ET.fromstring(zf.read(name))
        except ET.ParseError:
            continue
        for rel in rels_root.findall(f"{rel_ns}Relationship"):
            target = rel.get("Target") or ""
            if "drawing" in target.lower() and target.endswith(".xml"):
                # Target is like "../drawings/drawing1.xml" — keep basename
                basename = Path(target.replace("\\", "/")).name
                mapping[basename] = sheet_no
    return mapping


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def xlsx_connector_hook(
    file_path: Path,
    parse_result: ParseResult,
    config: dict[str, Any],
) -> None:
    """
    post_parse hook: extract AutoShape connector relationships from xlsx
    OOXML and inject as Vision structured_data.

    Failure modes (all → HookNoOp, silent skip):
      - feature disabled in config
      - file is not a real xlsx (zip can't open or no xl/drawings/)
      - no drawings contain any connectors
      - parsing all drawings yielded zero edges within attach threshold
    """
    if not get_nested(config, "parsing.xlsx.extract_connector_data", True):
        raise HookNoOp("xlsx connector extraction disabled in config")

    # Only run on real .xlsx (Phase 0.5 will have converted .xls already).
    suffix = file_path.suffix.lower()
    if suffix != ".xlsx":
        raise HookNoOp(f"unsupported extension for connector extraction: {suffix}")

    try:
        with zipfile.ZipFile(file_path) as zf:
            drawing_names = [
                n for n in zf.namelist()
                if n.startswith("xl/drawings/") and n.endswith(".xml")
                and "_rels" not in n
            ]
            if not drawing_names:
                raise HookNoOp("no drawing XMLs in xlsx")

            sheet_map = _read_drawing_sheet_map(zf)

            # Aggregate edges per sheet index. A sheet may have one drawing,
            # but we key by sheet because Vision pages are sheet-aligned.
            per_sheet_edges: dict[int, list[dict]] = {}
            total_edges = 0

            for dname in drawing_names:
                try:
                    xml_text = zf.read(dname).decode("utf-8", errors="replace")
                except (KeyError, zipfile.BadZipFile):
                    continue
                boxes, connectors = _parse_drawing(xml_text)
                if not connectors:
                    continue
                edges = _reconstruct_edges(boxes, connectors)
                if not edges:
                    continue
                basename = Path(dname).name
                sheet_idx = sheet_map.get(basename)
                if sheet_idx is None:
                    # Drawing not bound to any sheet rels — skip rather than guess.
                    continue
                per_sheet_edges.setdefault(sheet_idx, []).extend(edges)
                total_edges += len(edges)
    except (zipfile.BadZipFile, OSError) as e:
        # Encrypted / corrupted / not-actually-a-zip → silent skip.
        raise HookNoOp(f"xlsx unreadable: {e}") from e

    if total_edges == 0:
        raise HookNoOp("no connector relationships reconstructed")

    # Convert sheet index → page number. For the common single-page-per-sheet
    # case this is identity (sheet 1 = page 1). Multi-page-per-sheet (long
    # 方眼紙 layouts) lands all edges on the FIRST page of that sheet — Vision
    # for later pages still has the rendered image to fall back on.
    extractions: dict[int, str] = parse_result.metadata.setdefault(
        "structured_extractions_per_page", {}
    )
    for sheet_idx, edges in per_sheet_edges.items():
        md_block = _format_edges_as_markdown(edges)
        if not md_block:
            continue
        page_no = sheet_idx  # 1-based, identity mapping for 1 sheet = 1 page
        if page_no in extractions:
            extractions[page_no] = extractions[page_no] + "\n\n" + md_block
        else:
            extractions[page_no] = md_block

    parse_result.metadata["xlsx_connectors_extracted"] = total_edges
    logger.info(
        f"xlsx connector extraction: {total_edges} edge(s) reconstructed "
        f"across {len(per_sheet_edges)} sheet(s) of {file_path.name}"
    )
