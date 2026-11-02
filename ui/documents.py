"""Read-only access to vault document files: listing, and cached thumbnails and page renders."""

from __future__ import annotations

import threading
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
from PIL import Image, ImageOps

from config import UIConfig

# PDFium is not thread-safe, and the gateway renders pages from worker threads.
PDFIUM_LOCK = threading.Lock()


def render_page(path: Path, index: int, max_edge: int) -> Image.Image:
    """Render page ``index`` of a PDF, or the image itself, as RGB on white."""
    if path.suffix.lower() != ".pdf":
        if index != 0:
            raise IndexError(index)
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
    """Pair backend metadata with the vault's files, and render pictures of them once."""

    def __init__(self, document_dir: Path) -> None:
        self.document_dir = document_dir
        self.thumbnails: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self.pages: OrderedDict[tuple[str, int, int, str], bytes] = OrderedDict()

    def path(self, name: str) -> Path:
        """The vault file called ``name``; FileNotFoundError for anything else."""
        path = self.document_dir / name
        if (
            Path(name).name != name
            or path.suffix.lower() not in UIConfig.DOCUMENT_EXTENSIONS
            or not path.is_file()
        ):
            raise FileNotFoundError(name)
        return path

    @staticmethod
    def _remember(cache: OrderedDict, key: Any, value: bytes, limit: int) -> bytes:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
        return value

    def collection(self, metadata: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Records with files on disk, newest first."""
        records = []
        for item in metadata:
            try:
                stat = self.path(item["name"]).stat()
            except OSError:
                continue
            records.append(
                {**item, "size_bytes": item.get("size_bytes") or stat.st_size, "version": stat.st_mtime_ns}
            )
        records.sort(key=lambda record: record["version"], reverse=True)
        return records

    def thumbnail(self, name: str) -> bytes:
        """The top of the first page, cropped to the card, encoded once per file version."""
        path = self.path(name)
        key = (name, path.stat().st_mtime_ns)
        cached = self.thumbnails.get(key)
        if cached is not None:
            self.thumbnails.move_to_end(key)
            return cached
        page = render_page(path, 0, UIConfig.THUMBNAIL_RENDER_EDGE)
        fitted = ImageOps.fit(page, UIConfig.THUMBNAIL_SIZE, method=Image.Resampling.LANCZOS, centering=(0.5, 0.0))
        encoded = BytesIO()
        fitted.save(encoded, "WEBP", quality=UIConfig.THUMBNAIL_QUALITY)
        return self._remember(self.thumbnails, key, encoded.getvalue(), UIConfig.THUMBNAIL_CACHE_ENTRIES)

    def page(self, name: str, index: int, size: str) -> bytes:
        """One readable page, rendered once per file version and size."""
        path = self.path(name)
        key = (name, path.stat().st_mtime_ns, index, size)
        cached = self.pages.get(key)
        if cached is not None:
            self.pages.move_to_end(key)
            return cached
        encoded = BytesIO()
        render_page(path, index, UIConfig.PAGE_EDGES[size]).save(encoded, "JPEG", quality=UIConfig.PAGE_QUALITY)
        return self._remember(self.pages, key, encoded.getvalue(), UIConfig.PAGE_CACHE_ENTRIES)
