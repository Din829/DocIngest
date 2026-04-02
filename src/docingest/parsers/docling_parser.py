"""
Docling parser — primary document parsing engine.

Wraps IBM Docling to handle PDF, PPTX, XLSX, HTML, images, and more.
Delegates all format routing to Docling internally (no manual extension matching).

If Docling fails, the pipeline falls back to TextParser (FallbackParser).

Design:
  - Lazy import: Docling is imported on first use (heavy library)
  - OCR config: mapped from our YAML to Docling's PdfPipelineOptions
  - Image extraction: Docling extracts images, we optionally describe them with Vision
  - Metadata: extracted from Docling's document model (title, pages, language, etc.)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .base import BaseParser, ParseResult
from ..config import get_nested

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OCR engine mapping: our config name → Docling option class
# ---------------------------------------------------------------------------

def _build_ocr_options(config: dict[str, Any]):
    """
    Build Docling OCR options from our config.

    Returns None if OCR should use Docling defaults.
    """
    ocr_cfg = get_nested(config, "parsing.ocr", {})
    engine = ocr_cfg.get("engine", "auto")
    languages = ocr_cfg.get("languages", ["eng"])

    if engine == "auto":
        # Let Docling pick the best available OCR engine
        return None

    # Map our engine names to Docling option classes
    try:
        if engine == "easyocr":
            from docling.datamodel.pipeline_options import EasyOcrOptions
            return EasyOcrOptions(lang=languages)
        elif engine == "tesseract":
            from docling.datamodel.pipeline_options import TesseractOcrOptions
            return TesseractOcrOptions(lang=languages)
        elif engine == "rapidocr":
            from docling.datamodel.pipeline_options import RapidOcrOptions
            return RapidOcrOptions()
        else:
            logger.warning(f"Unknown OCR engine '{engine}', using Docling default")
            return None
    except ImportError as e:
        logger.warning(f"OCR engine '{engine}' not available: {e}. Using Docling default.")
        return None


# ---------------------------------------------------------------------------
# Docling Parser
# ---------------------------------------------------------------------------

class DoclingParser(BaseParser):
    """
    Primary parser using IBM Docling.

    Handles 15+ document formats with AI layout analysis and table extraction.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._converter = None  # Lazy init

    def _get_converter(self):
        """Lazy-initialize DocumentConverter (heavy import)."""
        if self._converter is not None:
            return self._converter

        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.datamodel.base_models import InputFormat

        # Build PDF pipeline options from config
        pipeline_options = PdfPipelineOptions()

        # OCR
        ocr_cfg = get_nested(self.config, "parsing.ocr", {})
        pipeline_options.do_ocr = True
        if ocr_cfg.get("force", False):
            # Force OCR: bypass text layer, useful for broken PDFs
            # Attribute name varies by Docling version
            if hasattr(pipeline_options, "force_full_page_ocr"):
                pipeline_options.force_full_page_ocr = True
            elif hasattr(pipeline_options, "force_backend_text"):
                pipeline_options.force_backend_text = False  # Disable text layer → forces OCR

        ocr_options = _build_ocr_options(self.config)
        if ocr_options is not None:
            pipeline_options.ocr_options = ocr_options

        # Table extraction
        pipeline_options.do_table_structure = get_nested(
            self.config, "parsing.pdf.table_extraction", True
        )

        # Image extraction (generate images for Vision processing later)
        pipeline_options.generate_picture_images = get_nested(
            self.config, "parsing.pdf.image_extraction", True
        )

        # Build converter with PDF options
        self._converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=pipeline_options
                ),
            }
        )
        return self._converter

    def parse(self, file_path: Path) -> ParseResult:
        """Parse a document using Docling."""
        try:
            converter = self._get_converter()
            result = converter.convert(str(file_path))

            # Export to Markdown
            markdown = result.document.export_to_markdown()

            # Extract metadata
            metadata = self._extract_metadata(result, file_path)

            # Extract images (paths on disk)
            images = self._extract_images(result, file_path)

            return ParseResult(
                markdown=markdown,
                metadata=metadata,
                images=images,
                success=True,
            )
        except Exception as e:
            logger.error(f"Docling parse failed for {file_path.name}: {e}")
            return ParseResult(
                markdown="",
                success=False,
                error=f"Docling parse failed: {e}",
            )

    def _extract_metadata(self, result, file_path: Path) -> dict[str, Any]:
        """Extract document metadata from Docling result."""
        doc = result.document
        metadata: dict[str, Any] = {
            "format": file_path.suffix.lstrip(".").lower(),
            "title": file_path.stem,
        }

        # Try to get page count
        try:
            if hasattr(doc, "pages") and doc.pages:
                metadata["pages"] = len(doc.pages)
        except Exception:
            pass

        # Check for tables and images
        try:
            md_content = doc.export_to_markdown()
            metadata["has_tables"] = "|" in md_content and "---" in md_content
            metadata["has_images"] = "![" in md_content
        except Exception:
            pass

        return metadata

    def _extract_images(self, result, file_path: Path) -> dict[str, str]:
        """
        Extract image references from Docling result.

        Returns dict of asset_path → description (empty string, to be filled by Vision).
        """
        images: dict[str, str] = {}

        try:
            doc = result.document
            if hasattr(doc, "pictures") and doc.pictures:
                for i, pic in enumerate(doc.pictures):
                    # Build output asset path
                    asset_name = f"{file_path.stem}-img-{i:03d}.png"
                    images[f"assets/{asset_name}"] = ""

                    # Try to save image data if available
                    if hasattr(pic, "image") and pic.image is not None:
                        try:
                            assets_dir = Path(get_nested(
                                self.config, "output.dir", "./knowledge"
                            )) / get_nested(self.config, "output.assets_dir", "assets")
                            assets_dir.mkdir(parents=True, exist_ok=True)
                            output_path = assets_dir / asset_name
                            if hasattr(pic.image, "pil_image"):
                                pic.image.pil_image.save(str(output_path))
                            elif hasattr(pic.image, "save"):
                                pic.image.save(str(output_path))
                        except Exception as e:
                            logger.debug(f"Could not save image {asset_name}: {e}")
        except Exception as e:
            logger.debug(f"Image extraction failed for {file_path.name}: {e}")

        return images

    def supported_extensions(self) -> set[str]:
        return {
            ".pdf", ".docx", ".pptx", ".xlsx",
            ".html", ".htm",
            ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp", ".gif",
            ".md", ".txt", ".csv",
            ".asciidoc", ".adoc",
        }
