# -*- coding: utf-8 -*-
"""
Parse visualization — draw element bounding boxes onto rendered page images.

A debugging / QA tool (mirrors MinerU's draw_bbox, but on PNG not PDF: DocIngest
already renders page images, so PIL.ImageDraw on the existing PNG is far simpler
than MinerU's reportlab + pypdf overlay — and needs no PDF-rotation handling,
the rendered image is already upright).

Reads index.json + assets/ page images, overlays each element's bbox colored by
label, optionally numbered in reading order, writes annotated PNGs to a viz/ dir.

Coordinate mapping (the whole trick):
  docling bboxes are PDF points, origin BOTTOM-LEFT (y up); page images are
  top-left (y down). With the page's point size we get an exact scale:

      scale = image_px_width / page_point_width      (else fall back image_dpi/72)
      x_img = l * scale                              ;  x1 = r * scale
      y_img = image_px_height - t * scale            ;  y1 = image_px_height - b*scale
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Color per docling label (RGB). Single source of truth — tweak here.
_LABEL_COLORS: dict[str, tuple[int, int, int]] = {
    "text": (30, 120, 255),            # blue
    "section_header": (220, 30, 30),   # red
    "title": (220, 30, 30),            # red
    "table": (0, 170, 60),             # green
    "picture": (255, 140, 0),          # orange
    "list_item": (160, 30, 200),       # purple
    "caption": (0, 180, 180),          # teal
    "footnote": (140, 140, 140),       # gray
    "page_header": (150, 150, 150),    # gray
    "page_footer": (150, 150, 150),    # gray
    "formula": (200, 120, 0),          # amber
    "code": (120, 60, 200),            # violet
}
_DEFAULT_COLOR = (110, 110, 110)


def _draw_page(
    image: Any,
    boxes: list[dict],
    page_size: Optional[list[float]],
    image_dpi: int,
    labels_filter: Optional[set[str]],
    numbers: bool,
) -> int:
    """Draw boxes onto a PIL image in place. Returns the number of boxes drawn."""
    from PIL import ImageDraw, ImageFont

    w, h = image.size
    # Exact scale from page point-size; fall back to assuming the render DPI.
    if page_size and page_size[0]:
        scale = w / float(page_size[0])
    else:
        scale = image_dpi / 72.0

    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", max(14, int(h / 90)))
    except Exception:
        font = ImageFont.load_default()

    drawn = 0
    for idx, b in enumerate(boxes):
        label = b.get("label", "")
        if labels_filter and label not in labels_filter:
            continue
        bbox = b.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        l, t, r, bot = bbox
        x0, x1 = l * scale, r * scale
        y0, y1 = h - t * scale, h - bot * scale          # BOTTOM-LEFT → flip y
        color = _LABEL_COLORS.get(label, _DEFAULT_COLOR)
        draw.rectangle([x0, min(y0, y1), x1, max(y0, y1)], outline=color, width=3)
        if numbers:  # reading-order index at the box's top-left corner
            top_y = min(y0, y1)
            draw.text((x0 + 3, top_y + 2), str(idx + 1), fill=color, font=font)
        drawn += 1
    return drawn


def visualize_file(
    entry: dict,
    base_dir: Path,
    output_dir: Path,
    pages_filter: Optional[set[int]],
    labels_filter: Optional[set[str]],
    numbers: bool,
    image_dpi: int,
) -> list[tuple[Path, int]]:
    """Visualize one index.json file entry. Returns [(written_png, box_count), ...]."""
    from PIL import Image

    eb = entry.get("element_boxes")
    if not eb:
        return []
    page_sizes = entry.get("page_sizes") or {}
    stem = Path(entry.get("original_file", "")).stem
    assets = base_dir / "assets"

    written: list[tuple[Path, int]] = []
    for page_key in sorted(eb.keys(), key=lambda x: int(x)):
        page_no = int(page_key)
        if pages_filter and page_no not in pages_filter:
            continue
        boxes = eb[page_key]
        if not boxes:
            continue
        img_path = assets / f"{stem}-page-{page_no:03d}.png"
        if not img_path.exists():
            logger.debug(f"page image missing, skip: {img_path}")
            continue
        try:
            image = Image.open(img_path).convert("RGB")
        except Exception as e:
            logger.debug(f"open failed {img_path}: {e}")
            continue
        n = _draw_page(image, boxes, page_sizes.get(page_key), image_dpi, labels_filter, numbers)
        out_path = output_dir / f"{stem}-page-{page_no:03d}.png"
        image.save(out_path)
        written.append((out_path, n))
    return written


def visualize_knowledge(
    knowledge_dir: Path | str,
    pages: Optional[list[int]] = None,
    labels: Optional[list[str]] = None,
    numbers: bool = False,
    output_subdir: str = "viz",
    image_dpi: int = 180,
    index_file: str = "index.json",
) -> dict[str, Any]:
    """
    Visualize every file in a knowledge dir: read index.json + assets/, write
    annotated PNGs to ``<knowledge_dir>/<output_subdir>/``.

    Returns ``{"files": [...], "total_pages": N, "output_dir": str}``.
    Raises FileNotFoundError if index.json is absent.
    """
    base = Path(knowledge_dir)
    index_path = base / index_file
    if not index_path.exists():
        raise FileNotFoundError(f"index.json not found: {index_path}")
    index = json.loads(index_path.read_text(encoding="utf-8"))

    output_dir = base / output_subdir
    output_dir.mkdir(parents=True, exist_ok=True)

    labels_filter = set(labels) if labels else None
    pages_filter = set(pages) if pages else None

    results: list[dict[str, Any]] = []
    total = 0
    for entry in index.get("files", []):
        written = visualize_file(
            entry, base, output_dir, pages_filter, labels_filter, numbers, image_dpi
        )
        if written:
            results.append({
                "file": entry.get("original_file", ""),
                "has_page_sizes": bool(entry.get("page_sizes")),
                "pages": [{"path": str(p), "boxes": n} for p, n in written],
            })
            total += len(written)
    return {"files": results, "total_pages": total, "output_dir": str(output_dir)}
