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
from typing import Any, Sequence

from ..config import get_nested
from ..models.provider import describe_image, describe_images_batched
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

## Pre-extracted structured data (GROUND TRUTH — do not re-transcribe)

The following Markdown block was read **directly from the source document's \
object model** (e.g. PowerPoint's chart XML via python-pptx). It is \
**guaranteed accurate** — every number, label, and category name is \
authoritative. Your job:

1. **Do NOT re-transcribe the values or labels in this block.** They are \
already correct. Restating them wastes tokens and risks OCR errors.
2. **DO describe what this block does NOT contain:** visual annotations, \
arrows, callouts, highlighted regions, neighbouring text referencing the \
chart, colour legend semantics, manually added text boxes, and anything \
else you see on the page image that is NOT inside the block below.
3. **If the block below is "(none)" or empty**, act normally — transcribe \
the page image as usual per the rules above.
4. **If you notice the block is missing something important** (e.g. an axis \
unit visible in the image but absent from the block, or a chart type the \
block doesn't mention), YOU SHOULD add that detail — that's your job.
5. Reference the structured block naturally ("as shown in the table above") \
instead of repeating its content.

---
{structured_data}
---

Now examine the page image and produce the accurate Markdown for this page. \
Read aggressively; mark uncertainty explicitly; never invent content. \
Trust the structured data block — focus your effort on everything the block \
cannot see."""


def resolve_vision_config(
    config: dict[str, Any],
    doc_format: str | None,
) -> dict[str, Any]:
    """
    Merge global models.vision with parsing.<format>.vision override.

    Shallow merge for scalar fields (model, max_response_tokens, image_dpi).
    Subtree fields like triage do NOT participate — adjust those globally.
    Unset override fields fall through to the global config.
    """
    base = dict(get_nested(config, "models.vision", {}) or {})
    if doc_format:
        override = get_nested(
            config, f"parsing.{doc_format.lower()}.vision", {}
        ) or {}
        for key in ("model", "max_response_tokens", "image_dpi"):
            if key in override and override[key] is not None:
                base[key] = override[key]
    return base


def describe_page(
    image_path: str | Path,
    page_text: str,
    model_config: dict[str, Any],
    structured_data: str | None = None,
    max_tokens: int = 32768,
) -> str:
    """
    Send a page image + extracted text to Vision AI.

    The AI decides how to handle it based on the prompt.
    No code-level filtering or threshold logic.

    Args:
        image_path: Page screenshot.
        page_text: Docling's text extraction for this page (may be empty).
        model_config: models.vision dict.
        structured_data: Optional ground-truth Markdown block read
            directly from the source document's object model (e.g. PPTX
            chart data). When provided, the prompt instructs Vision to
            treat it as authoritative and focus on visual elements the
            block cannot capture. When None, the prompt renders "(none)"
            and Vision behaves as it did before this feature.
        max_tokens: Output cap forwarded to litellm. Set explicitly to
            bypass litellm's silent 4096 default for Gemini/Claude.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Page image not found: {image_path}")

    prompt = _PAGE_PROMPT.format(
        page_text=page_text if page_text.strip() else "(empty — no text extracted)",
        structured_data=structured_data if structured_data and structured_data.strip() else "(none)",
    )

    return describe_image(image_path, prompt, model_config, max_tokens=max_tokens)


def _structured_data_cache_tag(structured_data: str | None) -> str:
    """
    Stable short tag to fold into Vision cache keys so the cache invalidates
    when structured_data content changes. Truncated SHA256 is plenty for
    cache keying (not a security-sensitive hash).
    """
    import hashlib
    if not structured_data:
        return "nostruct"
    return "struct-" + hashlib.sha256(structured_data.encode("utf-8")).hexdigest()[:16]


def describe_page_cached(
    image_path: str | Path,
    page_text: str,
    config: dict[str, Any],
    cache: AICache | None = None,
    structured_data: str | None = None,
    doc_format: str | None = None,
) -> str:
    """
    Per-page Vision with caching.

    Cache key = image content hash + structured_data hash tag. When
    structured_data changes (hook extracted new/different content), the
    tag changes → cache miss → Vision re-runs. When structured_data is
    None or unchanged, cache behaviour is identical to before.

    doc_format (e.g. "pdf", "pptx") routes to parsing.<format>.vision
    overrides via resolve_vision_config. None → global config only.
    """
    image_path = Path(image_path)
    vision_model_config = resolve_vision_config(config, doc_format)
    # Fallback only fires when config is malformed (models.vision.max_response_tokens
    # missing AND no _defaults injection). Kept at the provider ceiling so a
    # misconfigured deployment still gets full 64K headroom, matching
    # config/default.yaml's explicit 65536 setting.
    max_tokens = int(vision_model_config.get("max_response_tokens", 65536))

    primary = vision_model_config.get("primary", {})
    model_name = f"{primary.get('provider', 'openai')}/{primary.get('model', 'gpt-4o-mini')}"

    if cache:
        img_hash = content_hash_file(image_path)
        return cache.get_or_call(
            model_name=model_name,
            content_hash=img_hash,
            call_fn=lambda: describe_page(
                image_path, page_text, vision_model_config, structured_data,
                max_tokens=max_tokens,
            ),
            extra_key=f"page_vision|{_structured_data_cache_tag(structured_data)}",
        )
    else:
        return describe_page(
            image_path, page_text, vision_model_config, structured_data,
            max_tokens=max_tokens,
        )


# ---------------------------------------------------------------------------
# Batched multi-image Vision (for xlsx whose single sheet spans many pages)
# ---------------------------------------------------------------------------

# Prompt for batched multi-image calls. Reuses the same uncertainty markers,
# anti-hallucination rules, and GROUND TRUTH semantics as _PAGE_PROMPT, but
# tells Vision the images are CONTIGUOUS pages of one logical sheet so it
# can stitch table continuations across page boundaries — the property that
# made batched calls beat per-page calls 90% vs 18% on coverage and 10/10
# vs 1/10 on continuity in the fair-comparison experiment.
_BATCHED_PROMPT = """\
You are a document preprocessing specialist. You receive {n_images} \
consecutive page images that all belong to the SAME logical sheet of an \
Excel/Office spreadsheet, in order (page 1 first, page N last). They may \
be continuations of one long table: a row visible at the bottom of page K \
can continue at the top of page K+1.

Your job: produce ONE accurate Markdown representation covering ALL pages, \
treating them as a single continuous document.

## CRITICAL: stitch across page breaks

- If a table spans pages, output it as ONE table — do not restart the
  header on every page, do not break the same logical row across two
  tables in your output.
- If a row is split visually (its first cell on page K, the rest on page
  K+1), merge them into one output row.
- Do NOT output per-page subtitles like "## Page 5". The pages are
  presentation artifacts; the logical structure is the spreadsheet.

## Uncertainty marker vocabulary (USE ONLY THESE — no other forms)

There are EXACTLY TWO symbols for flagging content you cannot read:

1. **`[?]`** — the ONLY marker for partially visible content.
2. **`[unreadable]`** — the ONLY marker for fully illegible content.

**FORBIDDEN alternative forms** — do NOT use these:
`[illegible]`, `[unclear]`, `[blurred]`, `[cut off]`, `[???]`, `???`,
ellipses, HTML comments, natural language phrases, confidence levels.

## Reading rules

- Read aggressively — small text, rotated text, colored text, margins all
  matter. You have strong OCR capability; use it.
- Partial reads with `[?]` beat `[unreadable]` or guessing.
- Use `[unreadable]` only when pixels are genuinely unreadable.

## Strict prohibitions

- **NEVER invent values** to fill blank cells. Empty stays empty.
- **NEVER guess** a number, date, name you cannot see clearly.
- **NEVER "complete" a partial value** by assuming what follows.

## Formatting rules

- Preserve original language exactly (日本語 stays 日本語).
- Preserve numbers/dates/amounts verbatim.
- Do NOT summarize, paraphrase, or interpret.
- Use compact Markdown.
- Do NOT wrap the entire output in a code block.
- Do NOT add "Here is the Markdown:" or similar preamble.

## Pre-extracted text from all pages (may be incomplete or garbled)

---
{page_text}
---

## Pre-extracted structured data (GROUND TRUTH — do not re-transcribe)

The following block was read directly from the source document's object \
model. It is **guaranteed accurate** — do NOT re-transcribe its values. \
DO describe anything visible in the images that is NOT in the block \
(annotations, arrows, highlights, etc.). If the block is "(none)", act \
normally and transcribe from the images.

---
{structured_data}
---

Now examine the {n_images} page images and produce ONE accurate, \
stitched-together Markdown representation. Read aggressively; mark \
uncertainty explicitly; never invent content."""


def describe_pages_batched(
    image_paths: "Sequence[Path | str]",
    page_texts: list[str],
    model_config: dict[str, Any],
    structured_data: str | None = None,
    max_tokens: int = 32768,
) -> str:
    """
    Send multiple page images of ONE logical sheet to Vision in a single
    API call.

    Args:
        image_paths: Ordered list of page screenshot files. Same order
            Vision sees them.
        page_texts: Per-page Docling-extracted text (same length as
            image_paths). Concatenated with "--- page N ---" separators
            into the prompt's "Pre-extracted text" section so Vision can
            consult OCR-extracted text per-page even though it sees one
            merged prompt.
        model_config: models.vision dict (post resolve_vision_config).
        structured_data: Optional ground-truth Markdown block. Same
            semantics as describe_page's structured_data — applies to
            ALL pages in this batch.
        max_tokens: Output cap forwarded to litellm.

    Returns:
        Single Vision response text covering all pages.
    """
    if not image_paths:
        return ""
    if len(image_paths) != len(page_texts):
        raise ValueError(
            f"image_paths ({len(image_paths)}) and page_texts "
            f"({len(page_texts)}) must have the same length"
        )

    # Concatenate per-page text with explicit page boundaries so Vision
    # can correlate OCR text with the corresponding image when stitching.
    joined_text = "\n\n".join(
        f"[page {i + 1} text]\n{t.strip() or '(empty)'}"
        for i, t in enumerate(page_texts)
    )

    prompt = _BATCHED_PROMPT.format(
        n_images=len(image_paths),
        page_text=joined_text,
        structured_data=structured_data if structured_data and structured_data.strip() else "(none)",
    )

    return describe_images_batched(
        image_paths, prompt, model_config, max_tokens=max_tokens,
    )


def describe_pages_batched_cached(
    image_paths: list[Path | str],
    page_texts: list[str],
    config: dict[str, Any],
    cache: AICache | None = None,
    structured_data: str | None = None,
    doc_format: str | None = None,
) -> str:
    """
    Batched multi-image Vision with caching.

    Cache key = model + ordered-image-hashes joined + structured_data tag.
    Different image order or different image set → cache miss → re-run.
    Identical batch (same images in same order) → cache hit, zero cost.

    The batched cache namespace ("batched_vision|...") is disjoint from
    the per-page namespace ("page_vision|..."), so switching between the
    two pipeline modes never poisons each other's cache.
    """
    paths = [Path(p) for p in image_paths]
    if not paths:
        return ""

    vision_model_config = resolve_vision_config(config, doc_format)
    max_tokens = int(vision_model_config.get("max_response_tokens", 32768))

    primary = vision_model_config.get("primary", {})
    model_name = f"{primary.get('provider', 'openai')}/{primary.get('model', 'gpt-4o-mini')}"

    if cache:
        # Combine all per-image content hashes in caller order — order
        # matters because Vision treats them as a sequence.
        import hashlib
        combined = hashlib.sha256(
            "|".join(content_hash_file(p) for p in paths).encode("utf-8")
        ).hexdigest()
        return cache.get_or_call(
            model_name=model_name,
            content_hash=combined,
            call_fn=lambda: describe_pages_batched(
                paths, page_texts, vision_model_config, structured_data,
                max_tokens=max_tokens,
            ),
            extra_key=f"batched_vision|n={len(paths)}|{_structured_data_cache_tag(structured_data)}",
        )
    else:
        return describe_pages_batched(
            paths, page_texts, vision_model_config, structured_data,
            max_tokens=max_tokens,
        )
