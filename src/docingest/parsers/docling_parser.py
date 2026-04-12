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
from io import BytesIO
from pathlib import Path
from typing import Any

from .base import BaseParser, ParseResult, PAGEBREAK_MARKER
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

        # Page-level image generation (for per-page Vision)
        vision_enabled = get_nested(self.config, "parsing.vision.enabled", True)
        if vision_enabled:
            pipeline_options.generate_page_images = True
            # Docling's images_scale is a multiplier of the PDF's native 72 DPI.
            # 180 DPI → scale 2.5 → 1488×2105 for A4, ~3.1 Mpx (under 4MP cap).
            image_dpi = get_nested(self.config, "parsing.vision.image_dpi", 180)
            pipeline_options.images_scale = image_dpi / 72.0

        # Build converter with PDF options
        self._converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=pipeline_options
                ),
            }
        )
        return self._converter

    def parse(
        self,
        file_path: Path,
        *,
        override_stream: BytesIO | None = None,
    ) -> ParseResult:
        """
        Parse a document using Docling.

        Args:
            file_path: Path to the input file (used for naming, metadata,
                and format detection even when override_stream is provided).
            override_stream: Optional BytesIO stream to feed Docling instead
                of reading file_path. Used by pre-parse hooks (e.g. DOCX OMML
                preprocessing) to transform file content before Docling sees
                it without touching the original file on disk.
        """
        from .base import PageData

        try:
            converter = self._get_converter()
            if override_stream is not None:
                # Route through DocumentStream so Docling reads the
                # transformed bytes while still seeing the original filename
                # (which it uses for format detection).
                from docling_core.types.io import DocumentStream
                override_stream.seek(0)
                doc_stream = DocumentStream(
                    name=file_path.name,
                    stream=override_stream,
                )
                result = converter.convert(doc_stream)
            else:
                result = converter.convert(str(file_path))
            doc = result.document

            # Export full Markdown (with page break markers)
            try:
                markdown = doc.export_to_markdown(
                    page_break_placeholder=PAGEBREAK_MARKER,
                )
            except TypeError:
                markdown = doc.export_to_markdown()

            # Inject section names from Docling groups (sheet names, slide titles, etc.)
            # This makes grep/search find content by real section names
            markdown = self._inject_section_names(doc, markdown)

            # Extract metadata
            metadata = self._extract_metadata(doc, file_path)

            # Build per-page data (text + image path) for Vision enrichment
            self._last_parse_metadata = {}
            pages = self._build_page_data(doc, file_path)

            # Merge xlsx embedded image paths into metadata (if any were extracted)
            if self._last_parse_metadata.get("xlsx_embedded_images"):
                metadata["xlsx_embedded_images"] = self._last_parse_metadata["xlsx_embedded_images"]

            # Extract per-element bounding boxes (if enabled)
            if get_nested(self.config, "output.include_bounding_boxes", True):
                metadata["element_boxes"] = self._extract_bounding_boxes(doc)

            # Detect hidden text via content_layer (if enabled, PDF only)
            if get_nested(self.config, "parsing.pdf.hidden_text_detection.enabled", True):
                hidden_info = self._detect_hidden_content(doc)
                if hidden_info["hidden_element_count"] > 0:
                    metadata["hidden_text"] = hidden_info

            return ParseResult(
                markdown=markdown,
                metadata=metadata,
                pages=pages,
                success=True,
            )
        except Exception as e:
            logger.error(f"Docling parse failed for {file_path.name}: {e}")
            return ParseResult(
                markdown="",
                success=False,
                error=f"Docling parse failed: {e}",
            )

    @staticmethod
    def _extract_bounding_boxes(doc) -> dict:
        """
        Extract per-element bounding boxes from Docling's Document model.

        Returns a dict organized by page number::

            {
                1: [
                    {"label": "text", "bbox": [l, t, r, b], "text_preview": "..."},
                    {"label": "table", "bbox": [l, t, r, b]},
                ],
                2: [...],
            }

        Coordinates are in Docling's coordinate system (origin depends on
        document, typically top-left for PDF). Only elements with provenance
        data are included.
        """
        boxes: dict[int, list[dict]] = {}
        try:
            for item, _level in doc.iterate_items():
                if not item.prov:
                    continue
                prov = item.prov[0]
                page_no = prov.page_no
                b = prov.bbox
                entry: dict = {
                    "label": item.label.value,
                    "bbox": [round(b.l, 1), round(b.t, 1), round(b.r, 1), round(b.b, 1)],
                }
                # Include short text preview for text elements (aids debugging)
                if hasattr(item, "text") and item.text:
                    entry["text_preview"] = item.text[:60]
                boxes.setdefault(page_no, []).append(entry)
        except Exception as e:
            logger.debug(f"Bounding box extraction failed: {e}")
        return boxes

    @staticmethod
    def _detect_hidden_content(doc) -> dict:
        """
        Detect hidden content using Docling's ContentLayer metadata.

        Checks each element's content_layer field — Docling marks some
        elements as INVISIBLE (e.g. Excel hidden sheets) or BACKGROUND
        (watermarks). This is a lightweight check that uses only the
        high-level Document API (no low-level PDF cell access needed).

        For deeper hidden text detection (rendering mode, font color vs
        background), PyMuPDF would be needed — but that's a heavier
        operation reserved for future enhancement.

        Returns::

            {
                "hidden_element_count": int,
                "background_element_count": int,
                "details": [{"page": int, "label": str, "layer": str, "preview": str}],
            }
        """
        from docling_core.types.doc.document import ContentLayer

        hidden_count = 0
        background_count = 0
        details: list[dict] = []
        try:
            for item, _level in doc.iterate_items():
                layer = item.content_layer
                if layer == ContentLayer.INVISIBLE:
                    hidden_count += 1
                    page_no = item.prov[0].page_no if item.prov else 0
                    preview = getattr(item, "text", "")[:60] if hasattr(item, "text") else ""
                    details.append({
                        "page": page_no,
                        "label": item.label.value,
                        "layer": "invisible",
                        "preview": preview,
                    })
                elif layer == ContentLayer.BACKGROUND:
                    background_count += 1
        except Exception as e:
            logger.debug(f"Hidden content detection failed: {e}")

        return {
            "hidden_element_count": hidden_count,
            "background_element_count": background_count,
            "details": details,
        }

    @staticmethod
    def _extract_group_names(doc) -> list[str]:
        """
        Extract section/group names from Docling document structure.

        Works for any format:
          - Excel: "sheet: Day5" → "Day5"
          - PPT: "slide-0" → "Slide 1"
          - PDF: may have chapter names
          - Others: whatever Docling provides

        Returns list of names aligned with pagebreak sections.
        """
        try:
            d = doc.export_to_dict()
        except Exception:
            return []

        groups = d.get("groups", [])
        names: list[str] = []

        for g in groups:
            raw_name = g.get("name", "")
            # Strip common prefixes (Docling convention)
            if raw_name.startswith("sheet: "):
                names.append(raw_name[7:])
            elif raw_name.startswith("slide-"):
                # "slide-0" → keep as-is or use children's first text
                names.append(raw_name)
            else:
                names.append(raw_name)

        return names

    @staticmethod
    def _inject_section_names(doc, markdown: str) -> str:
        """
        Inject group/section names as headings after each pagebreak.

        Before: content1 <!-- pagebreak --> content2
        After:  ## ドリルの実施方法\n\ncontent1 <!-- pagebreak -->\n\n## Day5\n\ncontent2

        Only injects if:
          1. Document has groups with meaningful names
          2. Markdown has pagebreaks matching group count
        """
        names = DoclingParser._extract_group_names(doc)
        if not names:
            return markdown

        if PAGEBREAK_MARKER not in markdown:
            # No pagebreaks — inject heading at top if there's a name
            if names and names[0]:
                return f"## {names[0]}\n\n{markdown}"
            return markdown

        sections = markdown.split(PAGEBREAK_MARKER)

        # Names should align with sections (1 name per section)
        # Section count may differ from name count (e.g., empty sections skipped)
        for i in range(min(len(sections), len(names))):
            name = names[i].strip()
            if not name:
                continue
            section = sections[i].strip()
            if section:
                sections[i] = f"\n## {name}\n\n{section}\n"
            else:
                sections[i] = f"\n## {name}\n"

        return PAGEBREAK_MARKER.join(sections)

    def _extract_metadata(self, doc, file_path: Path) -> dict[str, Any]:
        """Extract document metadata from Docling result."""
        # Suffix-based format is the baseline. For files with weak or
        # missing extensions (common after zip expansion, or for
        # renamed/downloaded files), the format detector can override
        # this via content-based identification (magika). Strong
        # extensions are trusted by default — see format_detector for
        # the decision logic.
        suffix_format = file_path.suffix.lstrip(".").lower()
        corrected_format: str | None = None
        try:
            from ..utils.format_detector import detect_format
            corrected_format = detect_format(file_path, self.config)
        except Exception as e:
            logger.debug(f"format_detector failed for {file_path.name}: {e}")

        metadata: dict[str, Any] = {
            "format": corrected_format or suffix_format,
            "title": file_path.stem,
        }
        if corrected_format and corrected_format != suffix_format:
            # Preserve the original suffix so debugging and the quality
            # report can see what magika overrode.
            metadata["suffix_format"] = suffix_format

        try:
            if hasattr(doc, "pages") and doc.pages:
                metadata["pages"] = len(doc.pages)
        except Exception:
            pass

        try:
            md_content = doc.export_to_markdown()
            metadata["has_tables"] = "|" in md_content and "---" in md_content
            metadata["has_images"] = "<!-- image" in md_content
        except Exception:
            pass

        # Surface Docling's own minimal metadata (DoclingDocument.origin +
        # name) so downstream hooks and the frontmatter writer can consume
        # them without re-parsing the file. Docling itself does NOT expose
        # author / creation date — those come from the exiftool hook.
        try:
            origin = getattr(doc, "origin", None)
            if origin is not None:
                docling_origin: dict[str, Any] = {}
                for field in ("filename", "mimetype", "binary_hash", "uri"):
                    value = getattr(origin, field, None)
                    if value:
                        docling_origin[field] = value
                if docling_origin:
                    metadata["docling_origin"] = docling_origin
            doc_name = getattr(doc, "name", None)
            if doc_name:
                metadata["docling_name"] = doc_name
        except Exception:
            pass

        return metadata

    def _build_page_data(self, doc, file_path: Path) -> list:
        """
        Build per-page data: extract text and save page image for each page.

        This gives the pipeline everything it needs for per-page Vision decisions.
        No filtering here — the pipeline/Vision prompt handles all logic.
        """
        from .base import PageData

        pages_data: list = []
        if not hasattr(doc, "pages") or not doc.pages:
            return pages_data

        assets_dir = Path(get_nested(
            self.config, "output.dir", "./knowledge"
        )) / get_nested(self.config, "output.assets_dir", "assets")
        assets_dir.mkdir(parents=True, exist_ok=True)

        for page_no, page in doc.pages.items():
            # Extract per-page text via Docling
            page_text = ""
            try:
                page_text = doc.export_to_markdown(page_no=page_no)
            except Exception:
                pass

            # Save page image (if available)
            image_path = ""
            if page.image is not None:
                pil_img = None
                if hasattr(page.image, "pil_image") and page.image.pil_image:
                    pil_img = page.image.pil_image
                elif hasattr(page.image, "save"):
                    pil_img = page.image

                if pil_img is not None:
                    asset_name = f"{file_path.stem}-page-{page_no:03d}.png"
                    output_path = assets_dir / asset_name
                    try:
                        pil_img.save(str(output_path))
                        image_path = str(output_path)
                    except Exception as e:
                        logger.debug(f"Could not save page image: {e}")

            pages_data.append(PageData(
                page_no=page_no,
                text=page_text,
                image_path=image_path,
            ))

        # Extract embedded images from xlsx zip (xl/media/)
        xlsx_denoise = get_nested(self.config, "parsing.xlsx.denoising", {})
        if xlsx_denoise.get("extract_images", True) and file_path.suffix.lower() in (".xlsx", ".xls"):
            embedded = self._extract_xlsx_images(file_path, assets_dir)
            if embedded:
                parse_meta = getattr(self, "_last_parse_metadata", {})
                parse_meta["xlsx_embedded_images"] = embedded
                self._last_parse_metadata = parse_meta

        # If no page images from Docling (e.g., PPT SimplePipeline),
        # try external conversion as fallback
        has_any_image = any(p.image_path for p in pages_data)
        if not has_any_image and pages_data:
            self._try_external_page_images(file_path, assets_dir, pages_data)

        return pages_data

    @staticmethod
    def _extract_xlsx_images(file_path: Path, assets_dir: Path) -> list[str]:
        """
        Extract embedded images from an xlsx file's zip structure.

        xlsx is a zip containing xl/media/image1.png, image2.jpeg, etc.
        These are the images pasted into cells (screenshots, diagrams).
        Works for ALL xlsx — data-heavy ones simply have no images (returns []).
        """
        import zipfile

        if not zipfile.is_zipfile(str(file_path)):
            return []

        image_exts = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".emf", ".wmf")
        extracted: list[str] = []

        try:
            with zipfile.ZipFile(str(file_path), "r") as zf:
                for name in zf.namelist():
                    if not name.startswith("xl/media/"):
                        continue
                    lower = name.lower()
                    if not any(lower.endswith(ext) for ext in image_exts):
                        continue
                    data = zf.read(name)
                    out_name = f"{file_path.stem}-{Path(name).name}"
                    out_path = assets_dir / out_name
                    out_path.write_bytes(data)
                    extracted.append(str(out_path))
        except Exception as e:
            logger.debug(f"xlsx image extraction failed: {e}")

        if extracted:
            logger.info(f"Extracted {len(extracted)} embedded images from {file_path.name}")
        return extracted

    def _try_external_page_images(
        self, file_path: Path, assets_dir: Path, pages_data: list
    ) -> None:
        """
        Fallback: convert document to page images using external tools.

        Tries LibreOffice headless → PDF → images pipeline.
        Gracefully does nothing if tools aren't available.
        """
        import subprocess
        import tempfile
        from ..utils.binary_finder import find_binary

        # Cross-platform LibreOffice lookup — handles Windows Program Files
        # installs, macOS /Applications bundles, and config/env overrides.
        soffice = find_binary("soffice", self.config)
        if not soffice:
            logger.debug("LibreOffice not found — skipping PPT page image export")
            return

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                # Step 1: Convert to PDF via LibreOffice
                subprocess.run(
                    [soffice, "--headless", "--convert-to", "pdf",
                     "--outdir", tmpdir, str(file_path)],
                    capture_output=True, timeout=120,
                )
                pdf_files = list(Path(tmpdir).glob("*.pdf"))
                if not pdf_files:
                    return

                # Resolve target DPI from config (unified across all paths)
                image_dpi = get_nested(self.config, "parsing.vision.image_dpi", 180)

                # Step 2: PDF pages → images via Docling or pdf2image
                try:
                    from pdf2image import convert_from_path
                    images = convert_from_path(str(pdf_files[0]), dpi=image_dpi)
                    for i, img in enumerate(images):
                        if i < len(pages_data):
                            name = f"{file_path.stem}-page-{pages_data[i].page_no:03d}.png"
                            out = assets_dir / name
                            img.save(str(out))
                            pages_data[i].image_path = str(out)
                except ImportError:
                    # pdf2image not available — try poppler-free approach
                    # Re-parse the PDF with Docling to get page images
                    from docling.document_converter import DocumentConverter, PdfFormatOption
                    from docling.datamodel.pipeline_options import PdfPipelineOptions
                    from docling.datamodel.base_models import InputFormat

                    opts = PdfPipelineOptions()
                    opts.generate_page_images = True
                    opts.images_scale = image_dpi / 72.0
                    conv = DocumentConverter(
                        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
                    )
                    result = conv.convert(str(pdf_files[0]))
                    for page_no, page in result.document.pages.items():
                        if page.image is None:
                            continue
                        pil = getattr(page.image, "pil_image", None)
                        if pil is None:
                            continue
                        # Match page_no to pages_data index
                        idx = page_no - 1  # Docling pages are 1-indexed
                        if 0 <= idx < len(pages_data):
                            name = f"{file_path.stem}-page-{pages_data[idx].page_no:03d}.png"
                            out = assets_dir / name
                            pil.save(str(out))
                            pages_data[idx].image_path = str(out)

        except Exception as e:
            logger.debug(f"External page image conversion failed: {e}")

    def supported_extensions(self) -> set[str]:
        return {
            ".pdf", ".docx", ".pptx", ".xlsx",
            ".html", ".htm",
            ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp", ".gif",
            ".md", ".txt", ".csv",
            ".asciidoc", ".adoc",
        }
