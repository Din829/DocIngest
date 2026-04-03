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
_PAGE_PROMPT = """\
You are a document preprocessing assistant. You receive:
1. A page image from a document (PDF, PPT, etc.)
2. Text that was extracted from this page by an OCR/parsing engine (may be incomplete, garbled, or empty)

Your job: produce the BEST possible Markdown representation of this page's content.

Rules:
- If the extracted text is already complete and accurate, return it cleaned up (fix formatting only)
- If the page has charts, graphs, diagrams, or images, describe them in detail with all data points
- If the extracted text is empty or garbled (OCR failure), read the page image yourself and extract all text
- If the page has both good text AND visual elements, combine: keep the text + add descriptions for visuals
- Output clean Markdown. Use tables for tabular data, lists for bullet points, headings for titles
- Preserve the original language of the document (Japanese, Chinese, English, etc.)
- Be precise with numbers, percentages, dates, and proper nouns
- Do NOT add commentary — only output the page content

Extracted text from this page (may be incomplete):
---
{page_text}
---

Now look at the page image and produce the best Markdown for this page."""


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
