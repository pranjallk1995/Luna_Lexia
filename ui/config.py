"""Validated presentation and service settings for the Streamlit UI."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse


@dataclass(frozen=True)
class UIConfig:
    """Immutable UI layout and backend-connection settings."""

    UI_ROOT: ClassVar[Path] = Path(__file__).resolve().parent
    LOGO_PATH: ClassVar[Path] = UI_ROOT / "assets" / "lunalexia_logo.png"
    MAX_TAGS: ClassVar[int] = 32
    # Word and Excel files are converted to PDF by the backend and stored as PDFs.
    UPLOAD_TYPES: ClassVar[tuple[str, ...]] = ("pdf", "png", "jpg", "jpeg", "webp", "docx", "doc", "xlsx", "xls")

    document_dir: Path
    api_url: str
    api_token: str
    allowed_emails: frozenset[str]
    # Portrait-ish cards show the top of the first page, where the letterhead is.
    card_size: tuple[int, int] = (420, 300)
    page_preview_edge: int = 1400
    gallery_corner_radius: int = 10
    gallery_selection_color: str = "#D4D4D4"
    gallery_border_thickness: int = 4
    gallery_max_row_height: int = 240
    gallery_columns: int = 3
    documents_per_page: int = 12
    document_extensions: frozenset[str] = frozenset({".jpeg", ".jpg", ".pdf", ".png", ".webp"})

    @classmethod
    def from_env(cls) -> "UIConfig":
        """Build and validate UI configuration from environment variables."""
        api_url = os.getenv("LUNALEXIA_API_URL", "http://luna-lexia-mcp:8000").rstrip("/")
        parsed = urlparse(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("LUNALEXIA_API_URL must be a valid HTTP(S) URL.")
        api_token = os.getenv("LUNALEXIA_API_TOKEN", "").strip()
        if len(api_token) < 32:
            raise RuntimeError("LUNALEXIA_API_TOKEN must contain at least 32 characters.")
        allowed_emails = frozenset(
            email.strip().casefold()
            for email in os.getenv("LUNALEXIA_ALLOWED_EMAILS", "").split(",")
            if email.strip()
        )
        if not allowed_emails:
            raise RuntimeError("LUNALEXIA_ALLOWED_EMAILS must list at least one account.")
        return cls(
            document_dir=Path(os.getenv("DOCUMENT_DIR", "/data/documents")),
            api_url=api_url,
            api_token=api_token,
            allowed_emails=allowed_emails,
        )
