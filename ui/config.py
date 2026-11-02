"""Validated settings for the UI gateway and the browser app it serves."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse


@dataclass(frozen=True)
class SSOConfig:
    """Single sign-on through Luna Sentia; this block is identical in every Luna app's config.py."""

    COOKIE_NAME: ClassVar[str] = "luna_sso"
    SESSION_HEADER: ClassVar[str] = "X-Luna-Session"
    # How long a confirmed session is trusted before asking Sentia again, so a sign-out
    # in another app takes effect here within this many seconds.
    RECHECK_SECONDS: ClassVar[int] = 60
    LOOKUP_TIMEOUT_SECONDS: ClassVar[float] = 5.0
    SESSION_CACHE_ENTRIES: ClassVar[int] = 256

    public_url: str
    internal_url: str = "http://luna-sentia-auth:8000"

    @classmethod
    def from_env(cls) -> "SSOConfig":
        public_url = os.getenv("LUNA_SSO_URL", "").strip().rstrip("/")
        if not public_url:
            raise RuntimeError("LUNA_SSO_URL must be the public address of Luna Sentia's auth service.")
        internal_url = os.getenv("LUNA_SSO_INTERNAL_URL", "").strip().rstrip("/")
        return cls(public_url=public_url, internal_url=internal_url or cls.internal_url)


@dataclass(frozen=True)
class UIConfig:
    """Immutable gateway settings, plus what the browser app reads from ``/auth/session``."""

    UI_ROOT: ClassVar[Path] = Path(__file__).resolve().parent
    # The built React app; the Dockerfile puts it here.
    STATIC_DIR: ClassVar[Path] = UI_ROOT / "static"
    MAX_TAGS: ClassVar[int] = 32
    NAME_MAX_CHARS: ClassVar[int] = 200
    TITLE_MAX_CHARS: ClassVar[int] = 120
    SUMMARY_MAX_CHARS: ClassVar[int] = 1000
    DOCUMENTS_PER_PAGE: ClassVar[int] = 12
    # Word and Excel files are converted to PDF by the backend and stored as PDFs.
    UPLOAD_TYPES: ClassVar[tuple[str, ...]] = ("pdf", "png", "jpg", "jpeg", "webp", "docx", "doc", "xlsx", "xls")
    DOCUMENT_EXTENSIONS: ClassVar[frozenset[str]] = frozenset({".jpeg", ".jpg", ".pdf", ".png", ".webp"})
    API_TIMEOUT_SECONDS: ClassVar[float] = 30.0
    IMPORT_TIMEOUT_SECONDS: ClassVar[float] = 60.0
    # Uploads and re-extraction wait for the backend's document model to read every page.
    EXTRACT_TIMEOUT_SECONDS: ClassVar[float] = 900.0
    # A 50 MiB document grows by a third as base64, plus its JSON wrapper.
    MAX_REQUEST_BYTES: ClassVar[int] = 70 * 1024 * 1024
    # Twice the 420 x 300 card, showing the top of the first page, where the letterhead is.
    THUMBNAIL_SIZE: ClassVar[tuple[int, int]] = (840, 600)
    THUMBNAIL_RENDER_EDGE: ClassVar[int] = 1200
    THUMBNAIL_QUALITY: ClassVar[int] = 80
    THUMBNAIL_CACHE_ENTRIES: ClassVar[int] = 256
    # Zoomed pages render sharper so small print stays legible at 300%.
    PAGE_EDGES: ClassVar[dict[str, int]] = {"preview": 1400, "zoom": 2400}
    PAGE_QUALITY: ClassVar[int] = 85
    PAGE_CACHE_ENTRIES: ClassVar[int] = 64
    # Percent of the page column's width; 100 fits the page and needs no scrolling.
    ZOOM_LEVELS: ClassVar[tuple[int, ...]] = (100, 150, 200, 300)
    DRIVE_REFRESH_SECONDS: ClassVar[int] = 3
    RECENT_IMPORTS: ClassVar[int] = 5
    # Thumbnail, page, and file URLs carry the file's modified time, so they never go stale.
    IMMUTABLE_CACHE_CONTROL: ClassVar[str] = "private, max-age=31536000, immutable"
    CONTENT_SECURITY_POLICY: ClassVar[str] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )

    document_dir: Path
    api_url: str
    api_token: str
    allowed_emails: frozenset[str]
    sso: SSOConfig

    @classmethod
    def from_env(cls) -> "UIConfig":
        """Build and validate gateway configuration from environment variables."""
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
            sso=SSOConfig.from_env(),
        )
