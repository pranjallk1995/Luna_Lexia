"""Document byte validation, text and page reading, chat previews, and ZIP archives."""

import base64
import binascii
import math
import re
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image as PillowImage
from PIL import ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

# PDFium is not thread-safe, and tools run in worker threads, so every call is serialized.
PDFIUM_LOCK = threading.RLock()
PDF_SIGNATURE = b"%PDF-"
ZIP_SIGNATURE = b"PK\x03\x04"
# Legacy Word and Excel files, and password-protected modern ones, are OLE compound files.
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


@contextmanager
def open_pdf(data: bytes | Path, password: str | None = None) -> Iterator[pdfium.PdfDocument]:
    """Open a PDF under the PDFium lock and close it afterwards."""
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(data, password=password)
        try:
            yield document
        finally:
            document.close()


def is_pdf(data: bytes) -> bool:
    """PDF readers accept the signature anywhere in the first kilobyte."""
    return PDF_SIGNATURE in data[:1024]


class DocumentPayloadDecoder:
    """Decode document payloads, detect their real format, unlock PDFs, and check names.

    Base64 from LLM clients is often line-wrapped, unpadded, URL-safe, or a
    ``data:`` URL, so all of those are normalized before decoding.

    Word and Excel files are converted to PDF by ``converter`` and stored as
    that PDF, with a ``.pdf`` name.
    """

    FORMAT_EXTENSIONS = {"PDF": ".pdf", "JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
    OFFICE_EXTENSIONS = {".docx": "DOCX", ".doc": "DOC", ".xlsx": "XLSX", ".xls": "XLS"}
    OFFICE_MESSAGE = "Word and Excel files are not supported on this server."
    PROTECTED_OFFICE_MESSAGE = (
        "This Word or Excel file is password-protected. Save a copy without the password, "
        "or as a PDF, and upload that."
    )
    # Phone cameras write multi-picture JPEGs, which Pillow reports as MPO.
    FORMAT_ALIASES = {"MPO": "JPEG"}
    DEFAULT_STEM = "document"
    PASSWORD_REQUIRED_MESSAGE = (
        "This PDF is password-protected. Provide its password (Indian payslips and "
        "statements often use the PAN, date of birth, or a combination of both)."
    )

    def __init__(self, allowed_formats: dict[str, str], max_document_bytes: int, converter=None) -> None:
        self.allowed_formats = allowed_formats
        self.max_document_bytes = max_document_bytes
        self.converter = converter

    @property
    def max_base64_chars(self) -> int:
        """Return the largest Base64 payload allowed for the byte limit."""
        return 4 * ((self.max_document_bytes + 2) // 3)

    def decode_base64(self, payload: str) -> bytes:
        """Decode standard, URL-safe, wrapped, unpadded, or data-URL Base64."""
        text = payload.strip()
        if text[:5].casefold() == "data:":
            header, separator, text = text.partition(",")
            if not separator or not header.casefold().endswith(";base64"):
                raise ValueError("Document data URLs must contain Base64-encoded data.")
        text = re.sub(r"\s+", "", text)
        if len(text) > self.max_base64_chars:
            raise ValueError("Document content exceeds the 50 MiB limit.")
        text = text.replace("-", "+").replace("_", "/")
        text += "=" * (-len(text) % 4)
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("content_base64 must be valid base64 data.") from error

    @classmethod
    def office_format(cls, data: bytes) -> str | None:
        """Return ``DOCX``, ``DOC``, ``XLSX``, or ``XLS`` for Word and Excel bytes, else None."""
        if data.startswith(ZIP_SIGNATURE):
            try:
                with zipfile.ZipFile(BytesIO(data)) as archive:
                    names = set(archive.namelist())
            except zipfile.BadZipFile:
                return None
            return "DOCX" if "word/document.xml" in names else "XLSX" if "xl/workbook.xml" in names else None
        if data.startswith(OLE_SIGNATURE):
            if "EncryptedPackage".encode("utf-16-le") in data:
                raise ValueError(cls.PROTECTED_OFFICE_MESSAGE)
            if "WordDocument".encode("utf-16-le") in data:
                return "DOC"
            if "Workbook".encode("utf-16-le") in data or "Book".encode("utf-16-le") in data:
                return "XLS"
        return None

    def detect_format(self, data: bytes) -> str:
        """Return ``PDF``, a Word or Excel format, or the Pillow format of valid, bounded, supported bytes."""
        if not data or len(data) > self.max_document_bytes:
            raise ValueError("Document content is empty or exceeds the 50 MiB limit.")
        if is_pdf(data):
            return "PDF"
        office = self.office_format(data)
        if office:
            return office
        try:
            with PillowImage.open(BytesIO(data)) as image:
                detected = image.format
                image.verify()
        except (UnidentifiedImageError, OSError) as error:
            raise ValueError("Content is not a PDF, Word or Excel file, or supported image.") from error
        detected = self.FORMAT_ALIASES.get(detected, detected)
        if detected not in self.FORMAT_EXTENSIONS:
            supported = ", ".join([*self.FORMAT_EXTENSIONS, "DOCX", "DOC", "XLSX", "XLS"])
            raise ValueError(f"{detected} files are not supported; use {supported}.")
        return detected

    def convert(self, data: bytes, detected: str) -> tuple[bytes, str]:
        """Turn Word and Excel bytes into PDF bytes; return other formats unchanged."""
        if detected not in self.OFFICE_EXTENSIONS.values():
            return data, detected
        if self.converter is None:
            raise ValueError(self.OFFICE_MESSAGE)
        return self.converter.to_pdf(data, detected), "PDF"

    def stored_name(self, name: str) -> str:
        """Give a Word or Excel filename the ``.pdf`` extension it is stored under."""
        path = Path(name)
        return str(path.with_suffix(".pdf")) if path.suffix.lower() in self.OFFICE_EXTENSIONS else name

    def unlock(self, data: bytes, password: str | None = None) -> bytes:
        """Return PDF bytes that open without a password, verifying the PDF on the way.

        An unprotected PDF is returned unchanged (any password is ignored). A
        protected one is opened with ``password`` and re-saved without its
        security, so the vault can read it again later without the password.
        """
        try:
            with open_pdf(data):
                return data
        except pdfium.PdfiumError as error:
            if getattr(error, "err_code", None) != pdfium_c.FPDF_ERR_PASSWORD:
                raise ValueError("Content is not a valid PDF.") from error
        if not password:
            raise ValueError(self.PASSWORD_REQUIRED_MESSAGE)
        try:
            with open_pdf(data, password) as document:
                output = BytesIO()
                document.save(output, flags=pdfium_c.FPDF_REMOVE_SECURITY)
        except pdfium.PdfiumError as error:
            raise ValueError("The PDF password is incorrect.") from error
        return output.getvalue()

    def prepare(self, name: str, data: bytes, password: str | None = None) -> bytes:
        """Require ``data`` to match ``name``'s format; return the bytes to store."""
        data, detected = self.convert(data, self.detect_format(data))
        expected = self.allowed_formats[Path(name).suffix.lower()]
        if detected != expected:
            raise ValueError(f"Document content is {detected}, filename expects {expected}.")
        return self.unlock(data, password) if detected == "PDF" else data

    def name_for(self, requested: str | None, detected: str) -> str:
        """Turn a requested name into a plain filename whose extension matches.

        Directories are dropped, and a missing or wrong extension is replaced
        with the detected format's (``scan.png`` holding a PDF -> ``scan.pdf``).
        """
        name = Path((requested or "").strip().replace("\\", "/")).name
        stem, suffix = Path(name).stem, Path(name).suffix.lower()
        if suffix not in self.allowed_formats and suffix not in self.OFFICE_EXTENSIONS:
            stem = name
        stem = stem.strip().lstrip(".")
        if not stem:
            stem = self.DEFAULT_STEM
        if suffix in self.allowed_formats and self.allowed_formats[suffix] == detected:
            return f"{stem}{Path(name).suffix}"
        return f"{stem}{self.FORMAT_EXTENSIONS[detected]}"


UPLOAD_PREFIX = re.compile(r"[0-9a-f]{32}_(?=.)", re.IGNORECASE)


def display_name(stored_name: str) -> str:
    """Drop the internal upload prefix (32 hex characters and ``_``) from a filename."""
    return UPLOAD_PREFIX.sub("", stored_name, count=1)


class DocumentReader:
    """Read page renders, the raw PDF text layer, and file properties from document bytes.

    Images are treated as one-page documents without a text layer. Full parsing
    (OCR, tables, layout) is Docling's job; see ``app.integrations.parsing``.
    """

    METADATA_KEYS = ("Title", "Author", "Subject", "Keywords", "Creator", "Producer", "CreationDate", "ModDate")
    # Below this many letters and digits a PDF is treated as a scan.
    MIN_TEXT_CHARACTERS = 40

    @staticmethod
    def file_format(data: bytes) -> str:
        if is_pdf(data):
            return "PDF"
        with PillowImage.open(BytesIO(data)) as image:
            return DocumentPayloadDecoder.FORMAT_ALIASES.get(image.format, image.format)

    @staticmethod
    def text_layer(data: bytes) -> list[str]:
        """Return the text PDFium finds on each page (empty for scans and images)."""
        if not is_pdf(data):
            return [""]
        pages = []
        with open_pdf(data) as document:
            for page in document:
                textpage = page.get_textpage()
                pages.append(textpage.get_text_bounded().replace("\r\n", "\n"))
                textpage.close()
                page.close()
        return pages or [""]

    def page_count(self, data: bytes) -> int:
        if not is_pdf(data):
            return 1
        with open_pdf(data) as document:
            return len(document)

    @staticmethod
    def _flatten(image: PillowImage.Image) -> PillowImage.Image:
        """Return an RGB image, putting any transparency on white paper."""
        image = ImageOps.exif_transpose(image)
        if image.mode in {"RGBA", "LA", "P"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            paper = PillowImage.new("RGB", rgba.size, "white")
            paper.paste(rgba, mask=rgba.getchannel("A"))
            return paper
        return image.convert("RGB")

    def render_page(self, data: bytes, index: int, max_edge: int) -> PillowImage.Image:
        """Render page ``index`` (0-based) so its longer side is at most ``max_edge``."""
        if not is_pdf(data):
            if index != 0:
                raise ValueError("Image documents have only page 1.")
            with PillowImage.open(BytesIO(data)) as source:
                source.draft("RGB", (max_edge, max_edge))
                image = self._flatten(source)
            image.thumbnail((max_edge, max_edge), PillowImage.Resampling.LANCZOS)
            return image
        with open_pdf(data) as document:
            if not 0 <= index < len(document):
                raise ValueError(f"Page {index + 1} does not exist; the document has {len(document)} pages.")
            page = document[index]
            width, height = page.get_size()
            image = page.render(scale=max_edge / max(width, height, 1)).to_pil().convert("RGB")
            page.close()
            return image

    @staticmethod
    def jpeg(image: PillowImage.Image, quality: int = 82) -> bytes:
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue()

    def page_jpegs(self, data: bytes, count: int, max_edge: int) -> list[bytes]:
        """Render up to the first ``count`` pages as JPEG bytes for a vision model."""
        pages = min(count, self.page_count(data))
        return [self.jpeg(self.render_page(data, index, max_edge), 88) for index in range(pages)]

    def properties(self, data: bytes) -> dict[str, Any]:
        """Describe the file itself: format, pages, size, and any embedded PDF metadata."""
        if not is_pdf(data):
            with PillowImage.open(BytesIO(data)) as image:
                return {
                    "format": DocumentPayloadDecoder.FORMAT_ALIASES.get(image.format, image.format),
                    "page_count": 1,
                    "width": image.width,
                    "height": image.height,
                    "dpi": [round(float(value)) for value in image.info["dpi"]] if "dpi" in image.info else None,
                    "has_text_layer": False,
                }
        text = "".join(self.text_layer(data))
        with open_pdf(data) as document:
            metadata = {
                key: value
                for key, value in document.get_metadata_dict(skip_empty=True).items()
                if key in self.METADATA_KEYS
            }
            width, height = document[0].get_size() if len(document) else (0, 0)
            version = document.get_version()
        return {
            "format": "PDF",
            "pdf_version": f"{version // 10}.{version % 10}" if version else None,
            "page_count": self.page_count(data),
            "first_page_size_mm": [round(width * 25.4 / 72), round(height * 25.4 / 72)],
            "has_text_layer": len(re.findall(r"\w", text)) >= self.MIN_TEXT_CHARACTERS,
            "metadata": metadata,
        }


class PreviewRenderer:
    """Render compact JPEG previews: one page, or a numbered sheet of first pages.

    Sheets use a near-square grid capped at ``SHEET_WIDTH`` with portrait tiles,
    so chat clients do not shrink them until labels become unreadable.
    """

    SHEET_WIDTH = 1536
    MAX_COLUMNS = 5
    TILE_ASPECT = 1.3
    TILE_PADDING = 12
    SHEET_BACKGROUND = "#111827"
    LABEL_COLOR = "#f5f5f5"

    def __init__(self, reader: DocumentReader, max_edge: int) -> None:
        self.reader = reader
        self.max_edge = max_edge

    @classmethod
    def columns_for(cls, count: int) -> int:
        return max(2, min(cls.MAX_COLUMNS, math.ceil(math.sqrt(count))))

    @staticmethod
    def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
        """Truncate ``text`` with an ellipsis so it fits ``width`` pixels."""
        if draw.textlength(text, font=font) <= width:
            return text
        while text and draw.textlength(f"{text}…", font=font) > width:
            text = text[:-1]
        return f"{text}…"

    def render_page(self, path: Path, page: int = 1) -> bytes:
        """Return one readable JPEG of ``page`` (1-based) of a document."""
        return self.reader.jpeg(self.reader.render_page(path.read_bytes(), page - 1, self.max_edge))

    def render(self, paths: list[Path], labels: list[str] | None = None) -> bytes:
        """Return JPEG bytes for one first page, or one sheet for several documents.

        Sheet tiles are numbered from 1 and labeled with ``labels`` (default:
        display names), so they can be matched to an accompanying text list.
        """
        if len(paths) == 1:
            return self.render_page(paths[0])

        labels = labels or [display_name(path.name) for path in paths]
        columns = self.columns_for(len(paths))
        tile_width = self.SHEET_WIDTH // columns
        page_height = int(tile_width * self.TILE_ASPECT)
        font = ImageFont.load_default(size=max(14, tile_width // 18))
        label_height = int(font.size * 1.9)
        row_height = page_height + label_height
        rows = math.ceil(len(paths) / columns)
        sheet = PillowImage.new(
            "RGB", (columns * tile_width, rows * row_height), self.SHEET_BACKGROUND
        )
        draw = ImageDraw.Draw(sheet)
        inner = (tile_width - 2 * self.TILE_PADDING, page_height - 2 * self.TILE_PADDING)
        for index, (path, label) in enumerate(zip(paths, labels)):
            column, row = index % columns, index // columns
            left, top = column * tile_width, row * row_height
            page = self.reader.render_page(path.read_bytes(), 0, max(inner) * 2)
            page.thumbnail(inner, PillowImage.Resampling.LANCZOS)
            sheet.paste(
                page,
                (left + (tile_width - page.width) // 2, top + (page_height - page.height) // 2),
            )
            text = self._fit(draw, f"{index + 1}. {label}", font, inner[0])
            draw.text(
                (left + self.TILE_PADDING, top + page_height + (label_height - font.size) // 2),
                text,
                fill=self.LABEL_COLOR,
                font=font,
            )
        return self.reader.jpeg(sheet)


class DocumentArchive:
    """Write original document files into a ZIP on disk.

    Entries are deflated: PDFs often contain uncompressed streams, unlike the
    already-compressed image formats.
    """

    @staticmethod
    def entry_name(stored_name: str) -> str:
        """Drop the internal upload prefix so entries keep their original names."""
        return display_name(stored_name)

    @classmethod
    def entry_names(cls, paths: list[Path]) -> list[str]:
        """Return unique entry names, numbering duplicates like ``form16 (2).pdf``."""
        names: list[str] = []
        taken: set[str] = set()
        for path in paths:
            name = cls.entry_name(path.name)
            stem, suffix = Path(name).stem, Path(name).suffix
            counter = 2
            while name.casefold() in taken:
                name = f"{stem} ({counter}){suffix}"
                counter += 1
            taken.add(name.casefold())
            names.append(name)
        return names

    @classmethod
    def write(cls, paths: list[Path], destination: Path) -> None:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path, name in zip(paths, cls.entry_names(paths)):
                bundle.write(path, arcname=name)
