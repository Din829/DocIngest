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
#   - Accurate > complete: hallucination is worse than a flagged gap
#   - Explicit uncertainty markers: [?] for partial, [unreadable] for gone
#   - Works for PDF, PPT, Excel, Word — any single-page document image
_PAGE_PROMPT = """\
You are a document preprocessing specialist. You receive one page image from a \
document (PDF / PPT / Excel / Word / etc.), plus text that was pre-extracted \
by an OCR/parsing engine (may be incomplete, garbled, or empty).

Your job: produce the most accurate Markdown representation of this ONE page.
Accuracy is more important than completeness. A hallucinated value is worse
than a gap marked with [unreadable]. Never invent content you cannot see.

## Uncertainty marker vocabulary (USE ONLY THESE — no other forms)

There are EXACTLY TWO symbols for flagging content you cannot read:

1. **`[?]`** — the ONLY marker for partially visible content.
   Replace the uncertain portion with `[?]` inline.
   Examples: `¥1,234,5[?]`, `invoice_20[?]`, `2024/0[?]/15`

2. **`[unreadable]`** — the ONLY marker for fully illegible content.
   Optional descriptive suffix with colon: `[unreadable: top-left node]`
   Used standalone when no context is helpful: `[unreadable]`

**FORBIDDEN alternative forms** — do NOT use these:
- `[illegible]`, `[unclear]`, `[blurred]`, `[cut off]`, `[???]`, `???`
- Ellipses `...` to indicate missing content
- HTML comments like `<!-- unreadable -->`
- Natural language phrases like "cannot be read" inline
- Confidence levels like "(low confidence)" or "(90%)"

These symbols are machine-scanned by a downstream quality report.
Using unsupported forms breaks that scan and hides problems.

## Reading rules (apply in this priority order)

### Priority 1: Read aggressively
**DEFAULT STANCE**: Make your best effort to transcribe every visible element.
Small text, rotated text, colored text, text in the margins or corners — all
of it matters. You have strong OCR capability; use it. Do not skip content
just because the text is small or the layout is unusual.

### Priority 2: Partial reads are valuable
If you can make out some of a value but not all:
- Write what you can read + [?] for the uncertain part
- Example: "¥1,234,5[?]" when the last digit is blurred but you see the rest
- Example: "invoice_20[?]" when the year is clear but the month/day is smudged
- A partial read with [?] is MORE useful than [unreadable] or guessing

### Priority 3: [unreadable] is the last resort
Only use [unreadable] when the content is genuinely illegible:
- Pixel-level blur so severe you cannot identify any character
- Content obscured by another element (watermark, stamp)
- Text cut off outside the page boundary
- DO NOT use [unreadable] just because text is small — read it first
- DO NOT use [unreadable] because you're "not 100% sure" — if you can see it, write it

## Flowchart and diagram rules

- FIRST priority: read every node's label **verbatim**. Small labels still count.
- If a node label is genuinely illegible (see Priority 3):
  - Write the label as `[unreadable]`
  - AND describe the node's shape + position, e.g.
    "left-side rectangular node [unreadable], connected to central node"
  - This preserves structural information even when text is gone
- Always list every connection you can see: "A → B" or "A → [unreadable node]"
- Always list arrow labels verbatim if readable, or mark the connection as
  "unlabeled arrow" if the line carries no text

## Strict prohibitions

- **NEVER invent values** to fill blank or unclear cells. Empty → leave empty.
- **NEVER guess** a number, date, name, or amount you cannot see clearly.
- **NEVER "complete" a partially visible value** by assuming what follows.
- **NEVER use [unreadable] as an escape hatch** for content that's just small.

## Formatting rules

- Preserve the original language exactly (日本語 stays 日本語)
- Preserve numbers/dates/amounts verbatim (¥1,234, 15.3%, 2024/01/15)
- Do NOT summarize, paraphrase, or interpret content
- Do NOT echo the same fact twice
- Use compact Markdown: tables for tabular data, lists for enumerations,
  headings for titles, bold for emphasis
- Do NOT wrap the entire output in a code block
- Do NOT add "This page shows...", "Here is the Markdown:", or similar

## Decision logic (apply silently, do not explain)

- Extracted text is complete and accurate → clean formatting, return as Markdown
- Extracted text is garbled or empty → read the image yourself, transcribe every
  visible character (using [?] or [unreadable] per the rules above)
- Page has diagrams / charts / flowcharts / screenshots → follow the flowchart
  rules above: every readable label verbatim, mark only what's truly gone
- Multi-column layout → output in logical reading order, not visual order

## Output

- Start directly with page content. No preamble.
- No explanation. No "Here is the Markdown:". No quality commentary.

## Pre-extracted text (may be incomplete or garbled)

---
{page_text}
---

Now examine the page image and produce the accurate Markdown for this page. \
Read aggressively; mark uncertainty explicitly; never invent content."""


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
