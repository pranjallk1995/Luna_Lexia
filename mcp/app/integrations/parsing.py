"""Parse documents into per-page Markdown with Docling: text, OCR, tables, and layout."""

import logging
import re
import threading
from io import BytesIO
from pathlib import Path

from app.core.models import DocumentContent
from app.services.documents import PDFIUM_LOCK, DocumentReader
from app.core.config import AppConfig

logger = logging.getLogger(__name__)


class DoclingParser:
    """Turn PDF or image bytes into Markdown per page, OCR-ing scanned regions.

    Word and Excel files arrive here already converted to PDF.

    Docling keeps each item's page provenance, so pages are exported one by
    one and analysis prompts and reading tools can cite page numbers. Tables
    become Markdown tables. If Docling fails, the PDF text layer read by PDFium
    is used instead so an upload is never lost to a parser error.

    A page Docling returns empty although the PDF has a text layer there gets
    that text layer instead, so a parser miss never loses readable text.

    Docling pads table cells with spaces to align columns, and repeats a merged
    cell in every column it spans; on a Form 16 that is three quarters of the
    Markdown. ``compact_tables`` strips both, so the text stays within the
    model's prompt budget.

    Docling's layout, table, and OCR models load on first use, once per device.
    With ``device`` ``auto`` or ``cuda`` it uses the GPU, but gives way to the
    GPU's other users (Sentia's Ollama, Luna Vibia's CLAP): a document is read on
    the CPU when the GPU has less than ``AppConfig.DOCLING_GPU_MIN_FREE_MB`` free,
    or again on the CPU when the GPU runs out of memory partway, and the GPU's
    working memory is released after every document. Conversions run one at a
    time under the PDFium lock, because Docling's PDF backend also uses PDFium,
    which is not thread-safe.
    """

    NAME = "docling"
    FALLBACK_NAME = "pdfium"
    # Where the Dockerfile bakes the models; loading from here never contacts the
    # Hugging Face hub, which Docling's layout engine otherwise does on first use.
    IMAGE_PLACEHOLDER = re.compile(r"^\s*<!-- image -->\s*$", re.MULTILINE)
    SEPARATOR_CELL = re.compile(r"^:?-+:?$")
    LETTERS = re.compile(r"[A-Za-z]")

    def __init__(self, reader: DocumentReader, max_text_chars: int, device: str = "auto") -> None:
        self.reader = reader
        self.max_text_chars = max_text_chars
        self.device = device
        self._converters: dict[str, object] = {}
        self._converter_lock = threading.Lock()

    def converter(self, device: str):
        with self._converter_lock:
            if device not in self._converters:
                from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

                options = PdfPipelineOptions(
                    do_ocr=True,
                    do_table_structure=True,
                    accelerator_options=AcceleratorOptions(device=AcceleratorDevice(device)),
                    artifacts_path=AppConfig.DOCLING_ARTIFACTS_PATH if AppConfig.DOCLING_ARTIFACTS_PATH.is_dir() else None,
                )
                self._converters[device] = DocumentConverter(
                    allowed_formats=[InputFormat.PDF, InputFormat.IMAGE],
                    format_options={
                        InputFormat.PDF: PdfFormatOption(pipeline_options=options),
                        InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
                    },
                )
            return self._converters[device]

    @staticmethod
    def _gpu_free_mb() -> int | None:
        """Free GPU memory in MB, or None when torch sees no CUDA GPU."""
        import torch

        if not torch.cuda.is_available():
            return None
        return torch.cuda.mem_get_info()[0] // 2**20

    def _device_now(self) -> str:
        """The device for the next document: the GPU only while it has room to spare."""
        if self.device == "cpu":
            return "cpu"
        free = self._gpu_free_mb()
        if free is None:
            return "cpu"
        if free < AppConfig.DOCLING_GPU_MIN_FREE_MB:
            logger.info("The GPU has %d MB free; reading this document on the CPU.", free)
            return "cpu"
        return "cuda"

    @staticmethod
    def _release_gpu() -> None:
        """Hand Docling's cached GPU working memory back, keeping only its loaded models."""
        import torch

        torch.cuda.empty_cache()

    @staticmethod
    def _out_of_gpu_memory(error: BaseException | None, messages: list[str]) -> bool:
        """Whether a conversion failed for lack of GPU memory.

        Docling records a stage's failure on the page instead of raising, and wraps
        what it does raise, so the error chain and the recorded messages are searched.
        """
        while error is not None:
            messages.append(str(error))
            error = error.__cause__ or error.__context__
        return any("out of memory" in message.lower() for message in messages)

    def _bounded(self, pages: list[str]) -> list[str]:
        """Cut the pages so their total length stays within ``max_text_chars``."""
        bounded, remaining = [], self.max_text_chars
        for text in pages:
            bounded.append(text[: max(remaining, 0)])
            remaining -= len(text)
        return bounded or [""]

    def _convert(self, data: bytes, file_format: str, device: str):
        """The converted document, or None when the GPU ran out of memory."""
        from docling.datamodel.base_models import DocumentStream

        extension = ".pdf" if file_format == "PDF" else f".{file_format.lower()}"
        converter = self.converter(device)
        try:
            with PDFIUM_LOCK:
                result = converter.convert(DocumentStream(name=f"document{extension}", stream=BytesIO(data)))
        except Exception as error:
            if device == "cuda" and self._out_of_gpu_memory(error, []):
                return None
            raise
        finally:
            if device == "cuda":
                self._release_gpu()
        if device == "cuda" and self._out_of_gpu_memory(None, [item.error_message for item in result.errors]):
            return None
        return result.document

    def _docling_pages(self, data: bytes, file_format: str) -> list[str]:
        document = self._convert(data, file_format, self._device_now())
        if document is None:
            logger.warning("The GPU ran out of memory; reading this document again on the CPU.")
            document = self._convert(data, file_format, "cpu")
        page_count = max(len(document.pages), 1)
        return [
            self.compact_tables(self.IMAGE_PLACEHOLDER.sub("", document.export_to_markdown(page_no=number)).strip())
            for number in range(1, page_count + 1)
        ]

    @classmethod
    def compact_tables(cls, markdown: str) -> str:
        """Strip table cell padding, and collapse merged cells repeated across columns.

        A row counts as merged when most of its cells repeat their left
        neighbour; only text cells are collapsed there, so data rows (where two
        columns may legitimately hold the same value) keep every column.
        """
        lines = []
        for line in markdown.splitlines():
            if not line.startswith("|"):
                lines.append(line)
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(cls.SEPARATOR_CELL.fullmatch(cell) for cell in cells):
                lines.append("|" + "|".join("---" for _ in cells) + "|")
                continue
            repeats = sum(1 for left, cell in zip(cells, cells[1:]) if cell == left)
            if repeats * 2 >= len(cells):
                cells = [
                    cell
                    for index, cell in enumerate(cells)
                    if index == 0 or cell != cells[index - 1] or not cls.LETTERS.search(cell)
                ]
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    @staticmethod
    def _readable(text: str) -> bool:
        return bool(re.search(r"\w", text))

    def _with_text_layer(self, data: bytes, file_format: str, pages: list[str]) -> list[str]:
        """Fill pages Docling left empty from the PDF text layer, when it has text there."""
        if file_format != "PDF" or all(self._readable(page) for page in pages):
            return pages
        layer = self.reader.text_layer(data)
        return [
            page if self._readable(page) or index >= len(layer) else layer[index].strip()
            for index, page in enumerate(pages)
        ]

    def parse(self, data: bytes, file_format: str) -> DocumentContent:
        try:
            pages = self._with_text_layer(data, file_format, self._docling_pages(data, file_format))
            return DocumentContent(file_format, self._bounded(pages), self.NAME)
        except Exception:
            logger.exception("Docling could not parse the document; using the PDF text layer.")
            pages = self.reader.text_layer(data) if file_format == "PDF" else [""]
            return DocumentContent(file_format, self._bounded(pages), self.FALLBACK_NAME)
