"""Read-only access to vault document files with cached page rendering."""

import threading
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import streamlit as st
from PIL import Image, ImageOps

from config import UIConfig
from services.api import LunaLexiaAPI

# PDFium is not thread-safe, and Streamlit serves each session from its own thread.
PDFIUM_LOCK = threading.Lock()


def render_page(path: Path, index: int, max_edge: int) -> Image.Image:
    """Render page ``index`` of a PDF, or the image itself, as RGB on white."""
    if path.suffix.lower() != ".pdf":
        with Image.open(path) as source:
            source.draft("RGB", (max_edge, max_edge))
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA", "P"}:
                rgba = image.convert("RGBA")
                paper = Image.new("RGB", rgba.size, "white")
                paper.paste(rgba, mask=rgba.getchannel("A"))
                image = paper
            image = image.convert("RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        return image
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(path)
        try:
            page = document[index]
            width, height = page.get_size()
            image = page.render(scale=max_edge / max(width, height, 1)).to_pil().convert("RGB")
            page.close()
        finally:
            document.close()
    return image


class DocumentFiles:
    """List vault files and render cached thumbnails, pages, and facts."""

    def __init__(self, config: UIConfig) -> None:
        self.config = config
        self.document_dir = config.document_dir
        self.document_dir.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[Path]:
        """List supported vault files newest first without opening them."""
        return sorted(
            (
                path
                for path in self.document_dir.iterdir()
                if path.is_file() and path.suffix.lower() in self.config.document_extensions
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    def path(self, name: str) -> Path:
        return self.document_dir / name

    @staticmethod
    @st.cache_data(show_spinner=False, max_entries=256)
    def _thumbnail(document_path: str, modified_ns: int, size: tuple[int, int]) -> Image.Image:
        del modified_ns
        page = render_page(Path(document_path), 0, max(size) * 2)
        return ImageOps.fit(page, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.0))

    @staticmethod
    @st.cache_data(show_spinner=False, max_entries=64)
    def _page(document_path: str, modified_ns: int, index: int, max_edge: int) -> Image.Image:
        del modified_ns
        return render_page(Path(document_path), index, max_edge)

    def thumbnail(self, path: Path) -> Image.Image:
        """Return the cached top of the first page, cropped to the card size."""
        return self._thumbnail(str(path), path.stat().st_mtime_ns, self.config.card_size)

    def page(self, path: Path, index: int) -> Image.Image:
        """Return one cached, readable page render."""
        return self._page(str(path), path.stat().st_mtime_ns, index, self.config.page_preview_edge)

    @staticmethod
    def mime_type(path: Path) -> str:
        suffix = path.suffix.lower().lstrip(".")
        return "application/pdf" if suffix == "pdf" else f"image/{'jpeg' if suffix == 'jpg' else suffix}"

    @staticmethod
    def size_label(size: int) -> str:
        if size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        if size >= 1024:
            return f"{size / 1024:.0f} KB"
        return f"{size} B"

    @staticmethod
    def display_name(stored_name: str) -> str:
        """Remove the internal upload prefix from a user-facing filename."""
        prefix, separator, original_name = stored_name.partition("_")
        if (
            separator
            and len(prefix) == 32
            and all(character in "0123456789abcdef" for character in prefix.lower())
        ):
            return original_name
        return stored_name


@dataclass(frozen=True)
class DocumentCollection:
    """Vault files that have metadata in one collection, keyed by filename."""

    documents: list[Path]
    metadata_by_name: dict[str, dict]

    @classmethod
    def from_metadata(cls, files: DocumentFiles, metadata: list[dict]) -> "DocumentCollection":
        metadata_by_name = {item["name"]: item for item in metadata}
        return cls(
            [path for path in files.list() if path.name in metadata_by_name],
            metadata_by_name,
        )

    @classmethod
    def visible(cls, api: LunaLexiaAPI, files: DocumentFiles) -> "DocumentCollection":
        return cls.from_metadata(files, api.metadata())

    def only(self, names: set[str]) -> "DocumentCollection":
        return DocumentCollection(
            [path for path in self.documents if path.name in names], self.metadata_by_name
        )

    def matching(self, query: str) -> "DocumentCollection":
        """Keep documents whose name, title, issuer, type, year, or any tag contains ``query``."""
        if not query:
            return self

        def matches(path: Path) -> bool:
            metadata = self.metadata_by_name.get(path.name) or {}
            fields = [
                DocumentFiles.display_name(path.name),
                metadata.get("title"),
                metadata.get("issuer"),
                str(metadata.get("doc_type", "")).replace("_", " "),
                metadata.get("financial_year"),
                *metadata.get("tags", []),
            ]
            return any(query in str(field).casefold() for field in fields if field)

        return DocumentCollection(
            [path for path in self.documents if matches(path)], self.metadata_by_name
        )
