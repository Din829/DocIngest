"""
Vision module — per-page AI enrichment.

Design philosophy: NO code-level judgment about what needs Vision.
Every page gets sent to AI with its Docling-extracted text as context.
The AI decides what to do:
  - Text is complete? → Clean up and return.
  - Has charts/images? → Describe them.
  - Scanned/OCR garbage? → Re-OCR from the image.
  - Both text + charts? → Merge both.

The prompt does all the "thinking". Code just sends and receives.
Results are cached by content hash — same page = same result, zero cost.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import get_nested
from ..models.provider import describe_image
from ..models.cache import AICache, content_hash_file

logger = logging.getLogger(__name__)

# The prompt that makes AI the decision-maker, not the code.
# Design principles:
#   - AI decides what to do (no code-level thresholds or filtering)
#   - Lossless: every fact/number/name/label from the image must appear in output
#   - Concise: no meta-commentary, no rephrasing, no interpretation
#   - Works for PDF, PPT, Excel, Word — any single-page document image
_PAGE_PROMPT = """\
You are a document preprocessing specialist. You receive one page image from a \
document (PDF / PPT / Excel / Word / etc.), plus text that was pre-extracted \
by an OCR/parsing engine (may be incomplete, garbled, or empty).

Your job: produce the BEST possible Markdown representation of this ONE page.

## Core principles (in priority order)

1. **Lossless** — EVERY visible fact on the page must appear in your output:
   every number, date, percentage, proper noun, label, table cell, bullet,
   caption, footnote, diagram node, arrow label, button name, axis tick.
   Missing information is a failure. When in doubt, include it.

2. **Faithful** — Do not interpret, summarize, paraphrase, or add commentary.
   If the page says "Revenue grew 15%", write "Revenue grew 15%", not
   "The company performed well". Preserve the original language exactly.
   Preserve numbers exactly as written (¥1,234, 15.3%, 2024/01/15).

3. **Concise in form, not content** — Use compact Markdown structures:
   - Tables for tabular data (no prose descriptions of tables)
   - Bullet lists for enumerations
   - Headings (#, ##, ###) for titles and section boundaries
   - Bold for emphasized terms
   - Do NOT echo the same fact twice
   - Do NOT add "This page shows...", "The following describes...", etc.
   - Do NOT wrap the entire output in a code block

## Decision logic (apply silently, do not explain)

- **Extracted text is complete and accurate** → clean up whitespace/formatting,
  return as Markdown. Do not re-describe what's already written.
- **Extracted text is garbled or empty** → read the page image directly,
  transcribe every visible character yourself.
- **Page has diagrams/charts/flowcharts/screenshots** → describe them in
  prose BELOW the text, including every label, data point, arrow, legend,
  and annotation. For flowcharts: list every node and every transition.
  For tables in images: output as Markdown tables.
- **Page has both text AND visual elements** → combine both. Keep the
  extracted text + add precise descriptions for the visuals.
- **Multi-column layout** → output in logical reading order, not visual order.

## Output

- Start directly with the page content. No preamble.
- No explanation of what you did. No "Here is the Markdown:".
- No commentary about quality, completeness, or uncertainty.
- Just the Markdown.

## Pre-extracted text (may be incomplete or garbled)

---
{page_text}
---

Now examine the page image and produce the faithful, lossless, concise Markdown \
for this page."""


def describe_page(
    image_path: str | Path,
    page_text: str,
    model_config: dict[str, Any],
) -> str:
    """
    Send a page image + extracted text to Vision AI.

    The AI decides how to handle it based on the prompt.
    No code-level filtering or threshold logic.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Page image not found: {image_path}")

    prompt = _PAGE_PROMPT.format(page_text=page_text if page_text.strip() else "(empty — no text extracted)")

    return describe_image(image_path, prompt, model_config)


def describe_page_cached(
    image_path: str | Path,
    page_text: str,
    config: dict[str, Any],
    cache: AICache | None = None,
) -> str:
    """
    Per-page Vision with caching.

    Cache key includes image content hash, so same page = cache hit.
    """
    image_path = Path(image_path)
    vision_model_config = get_nested(config, "models.vision", {})

    primary = vision_model_config.get("primary", {})
    model_name = f"{primary.get('provider', 'openai')}/{primary.get('model', 'gpt-4o-mini')}"

    if cache:
        img_hash = content_hash_file(image_path)
        return cache.get_or_call(
            model_name=model_name,
            content_hash=img_hash,
            call_fn=lambda: describe_page(image_path, page_text, vision_model_config),
            extra_key="page_vision",
        )
    else:
        return describe_page(image_path, page_text, vision_model_config)
