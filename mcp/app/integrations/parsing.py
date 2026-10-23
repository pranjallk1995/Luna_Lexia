"""Parse documents into per-page Markdown with Docling: text, OCR, tables, and layout."""

import logging
import re
import threading
from io import BytesIO

from app.core.models import DocumentContent
from app.services.documents import PDFIUM_LOCK, DocumentReader

logger = logging.getLogger(__name__)


class DoclingParser:
    """Turn PDF or image bytes into Markdown per page, OCR-ing scanned regions.

    Docling keeps each item's page provenance, so pages are exported one by
    one and analysis prompts and reading tools can cite page numbers. Tables
    become Markdown tables. If Docling fails, the PDF text layer read by PDFium
    is used instead so an upload is never lost to a parser error.

    The converter loads its layout, table, and OCR models once, on first use.
    Conversions run one at a time under the PDFium lock, because Docling's PDF
    backend also uses PDFium, which is not thread-safe.
    """

    NAME = "docling"
    FALLBACK_NAME = "pdfium"
    IMAGE_PLACEHOLDER = re.compile(r"^\s*<!-- image -->\s*$", re.MULTILINE)

    def __init__(self, reader: DocumentReader, max_text_chars: int) -> None:
        self.reader = reader
        self.max_text_chars = max_text_chars
        self._converter = None
        self._converter_lock = threading.Lock()

    def converter(self):
        with self._converter_lock:
            if self._converter is None:
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

                options = PdfPipelineOptions(do_ocr=True, do_table_structure=True)
                self._converter = DocumentConverter(
                    allowed_formats=[InputFormat.PDF, InputFormat.IMAGE],
                    format_options={
                        InputFormat.PDF: PdfFormatOption(pipeline_options=options),
                        InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
                    },
                )
            return self._converter

    def _bounded(self, pages: list[str]) -> list[str]:
        """Cut the pages so their total length stays within ``max_text_chars``."""
        bounded, remaining = [], self.max_text_chars
        for text in pages:
            bounded.append(text[: max(remaining, 0)])
            remaining -= len(text)
        return bounded or [""]

    def _docling_pages(self, data: bytes, file_format: str) -> list[str]:
        from docling.datamodel.base_models import DocumentStream

        extension = ".pdf" if file_format == "PDF" else f".{file_format.lower()}"
        converter = self.converter()
        with PDFIUM_LOCK:
            document = converter.convert(DocumentStream(name=f"document{extension}", stream=BytesIO(data))).document
        page_count = max(len(document.pages), 1)
        return [
            self.IMAGE_PLACEHOLDER.sub("", document.export_to_markdown(page_no=number)).strip()
            for number in range(1, page_count + 1)
        ]

    def parse(self, data: bytes, file_format: str) -> DocumentContent:
        try:
            pages = self._docling_pages(data, file_format)
            return DocumentContent(file_format, self._bounded(pages), self.NAME)
        except Exception:
            logger.exception("Docling could not parse the document; using the PDF text layer.")
            pages = self.reader.text_layer(data) if file_format == "PDF" else [""]
            return DocumentContent(file_format, self._bounded(pages), self.FALLBACK_NAME)
