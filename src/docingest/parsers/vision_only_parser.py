"""
VisionOnlyParser — skip Docling parsing, render pages and let Vision do all the
reading. Selected with ``parsing.engine: vision_only``.

Why: on "page-IS-an-image" formats (PDF, scans, images) Docling's parse is both
the slow part and the part that hits the docling-parse Windows OOM bug — yet on
dense data-table pages its table output is often collapsed anyway, so teams end
up keeping only the Vision half (vision_keep=vision). This engine takes that to
its conclusion: don't parse with Docling at all. Render each page to an image
(PyMuPDF — fast, no OOM) and feed it to the existing per-page Vision step, which
already has a full-page transcription prompt. Everything downstream (write,
chunk, index) is unchanged because the output is the same ParseResult contract.

Scope — only PDF and image formats go vision-only. Every OTHER format is
DELEGATED to the normal Docling path, so this one engine value still does the
right thing per format:
  - xlsx  → still openpyxl (its accurate-table moat is NOT thrown at Vision)
  - docx / pptx / odt → still Docling (+ their LibreOffice page-image path)
  - txt / md / csv / html → still read as text (rendering them would be a
    pure downgrade)
  - audio / video → still ASR
This keeps the change additive and format-aware rather than a blunt override.

Cost note (honest, not hidden): vision-only means EVERY page is transcribed by
the Vision model — there is no Docling text, so the triage step that skips
text-only pages has nothing to judge and Vision runs on all pages. Wall-clock is
mitigated by the existing Vision concurrency pool, but token spend is higher than
the Docling+Vision-supplement path. You trade LLM cost for OOM-immunity and
(on collapsed-table pages) better table fidelity. Choose it deliberately.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import Any

from ..config import get_nested
from .base import BaseParser, ParseResult, PageData, PAGEBREAK_MARKER

logger = logging.getLogger(__name__)

# Formats that ARE a page image — these go vision-only. Everything else is
# delegated to the Docling path (see parse() below). Mirrors the PDF+image
# subset of _DOCLING_BINARY_FORMATS.
_PAGE_IMAGE_FORMATS: frozenset[str] = frozenset({
    ".pdf",
    ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp", ".gif",
})


class VisionOnlyParser(BaseParser):
    """Render pages → Vision reads them. Docling parse skipped for PDF/image."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        # Lazy delegate for non-page-image formats. Built on first use so a
        # pure-PDF run never constructs the Docling stack it won't touch.
        self._delegate: Any = None

    def _get_delegate(self) -> BaseParser:
        if self._delegate is None:
            from . import _DoclingWithFallback
            self._delegate = _DoclingWithFallback(self.config)
        return self._delegate

    def parse(
        self,
        file_path: Path,
        *,
        override_stream: BytesIO | None = None,
    ) -> ParseResult:
        suffix = file_path.suffix.lower()

        # Non-page-image format → delegate to the normal Docling path. This is
        # what makes one engine value correct for every format (xlsx stays
        # openpyxl, text stays text, audio stays ASR, etc.).
        if suffix not in _PAGE_IMAGE_FORMATS:
            return self._get_delegate().parse(file_path, override_stream=override_stream)

        if suffix == ".pdf":
            return self._parse_pdf(file_path)
        return self._parse_image(file_path)

    # -- PDF: render every page, leave text empty for Vision ----------------

    def _parse_pdf(self, file_path: Path) -> ParseResult:
        from .docling_parser import DoclingParser

        assets_dir = self._assets_dir()
        image_dpi = int(get_nested(self.config, "parsing.vision.image_dpi", 180))
        # Reuse the already-tested PyMuPDF renderer. {page_no: png_path}.
        page_images = DoclingParser._render_pdf_pages_pymupdf(
            file_path, assets_dir, image_dpi
        )
        if not page_images:
            # No pages rendered → genuine failure (corrupt PDF / PyMuPDF gone).
            # Fail loud; the pipeline records it and moves on. We do NOT silently
            # fall back to Docling — the user picked vision_only on purpose.
            return ParseResult(
                markdown="",
                success=False,
                error=f"vision_only: could not render any page of {file_path.name}",
                metadata={"error_type": "parse_error", "format": "pdf"},
            )

        pages: list[PageData] = []
        # text="" — Vision (Phase 1.5) transcribes the whole page from the image.
        # num_pictures=1 so triage always treats the page as visual (there is no
        # Docling text to prove otherwise) — i.e. never skip Vision on this path.
        for page_no in sorted(page_images):
            pages.append(PageData(
                page_no=page_no,
                text="",
                image_path=page_images[page_no],
                num_pictures=1,
            ))

        # Markdown is page-delimited placeholders; Phase 1.5 Vision fills each
        # page section in. PAGEBREAK_MARKER keeps page alignment for chunking and
        # for vision_keep's per-page logic.
        markdown = f"\n{PAGEBREAK_MARKER}\n".join("" for _ in pages)
        result = ParseResult(
            markdown=markdown,
            metadata={
                "format": "pdf",
                "title": file_path.stem,
                "pages": len(pages),
                "parser_engine": "vision_only",
            },
            pages=pages,
            success=True,
        )
        result.transformations.append(
            {"step": "parse", "name": "vision_only", "pages": len(pages)}
        )
        return result

    # -- single image: it already IS the page image -------------------------

    def _parse_image(self, file_path: Path) -> ParseResult:
        # An image input is its own page image — point Vision straight at it.
        # We copy it into assets so the artefact layout matches other formats
        # (sources/*.md + assets/), and so downstream paths that expect the
        # image under the output dir keep working.
        assets_dir = self._assets_dir()
        try:
            import shutil
            asset_name = f"{file_path.stem}-page-001{file_path.suffix.lower()}"
            dest = assets_dir / asset_name
            shutil.copyfile(file_path, dest)
            image_path = str(dest)
        except Exception as e:
            return ParseResult(
                markdown="",
                success=False,
                error=f"vision_only: could not stage image {file_path.name}: {e}",
                metadata={"error_type": "io_error", "format": file_path.suffix.lstrip(".").lower()},
            )

        pages = [PageData(page_no=1, text="", image_path=image_path, num_pictures=1)]
        result = ParseResult(
            markdown="",
            metadata={
                "format": file_path.suffix.lstrip(".").lower(),
                "title": file_path.stem,
                "pages": 1,
                "parser_engine": "vision_only",
            },
            pages=pages,
            success=True,
        )
        result.transformations.append({"step": "parse", "name": "vision_only", "pages": 1})
        return result

    def _assets_dir(self) -> Path:
        assets_dir = Path(get_nested(self.config, "output.dir", "./knowledge")) / get_nested(
            self.config, "output.assets_dir", "assets"
        )
        assets_dir.mkdir(parents=True, exist_ok=True)
        return assets_dir

    def supported_extensions(self) -> set[str]:
        # Reports the SAME set as the Docling path: for non-page-image formats it
        # delegates, so it can handle whatever Docling can. Page-image formats it
        # handles itself.
        return set(self._get_delegate().supported_extensions())
