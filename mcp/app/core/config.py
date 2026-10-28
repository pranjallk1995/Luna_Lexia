"""Validated runtime configuration for the Luna Lexia backend."""

import base64
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


@dataclass(frozen=True)
class AppConfig:
    """Immutable backend settings loaded from environment variables."""

    LOCAL_HOSTS: ClassVar[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})
    DOCLING_DEVICES: ClassVar[frozenset[str]] = frozenset({"auto", "cuda", "cpu"})
    # The UI's Streamlit secrets, mounted read-only; Drive import reuses its Google client.
    GOOGLE_OAUTH_FILE: ClassVar[Path] = Path("/run/secrets/google_oauth.toml")
    GOOGLE_AUTH_URI: ClassVar[str] = "https://accounts.google.com/o/oauth2/auth"
    GOOGLE_TOKEN_URI: ClassVar[str] = "https://oauth2.googleapis.com/token"
    DRIVE_STATE_MAX_AGE_SECONDS: ClassVar[int] = 900
    ASSET_DIR: ClassVar[Path] = Path(__file__).resolve().parent.parent / "assets"
    DOCLING_ARTIFACTS_PATH: ClassVar[Path] = Path("/root/.cache/docling/models")
    OFFICE_BINARY: ClassVar[str] = "soffice"
    OFFICE_TIMEOUT_SECONDS: ClassVar[int] = 120
    REMOTE_FETCH_TIMEOUT_SECONDS: ClassVar[float] = 30.0
    OLLAMA_TIMEOUT_SECONDS: ClassVar[float] = 600.0
    OLLAMA_CONNECT_TIMEOUT_SECONDS: ClassVar[float] = 10.0
    OLLAMA_MAX_REPLY_TOKENS: ClassVar[int] = 8192
    EMBEDDING_TIMEOUT_SECONDS: ClassVar[float] = 120.0
    EMBEDDING_BATCH: ClassVar[int] = 16
    # Largest page image edge sent to the model, and how much text classification reads.
    MODEL_IMAGE_EDGE: ClassVar[int] = 1600
    CLASSIFY_TEXT_CHARS: ClassVar[int] = 6000
    # Below this classification confidence a document is flagged for review.
    LOW_CONFIDENCE: ClassVar[float] = 0.6
    SEMANTIC_POLL_SECONDS: ClassVar[int] = 10
    SEMANTIC_BATCH_DOCUMENTS: ClassVar[int] = 8
    SEMANTIC_SNIPPET_CHARS: ClassVar[int] = 1500
    BATCH_MAX_DOCUMENTS: ClassVar[int] = 100
    # Leftover archives without a ticket are only removed once clearly abandoned, so a
    # concurrent purge never deletes an archive whose ticket is being written.
    DOWNLOAD_LEFTOVER_GRACE_SECONDS: ClassVar[int] = 60 * 60

    document_dir: Path
    database_url: str
    ollama_url: str
    document_model: str
    public_base_url: str = ""
    api_token: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    allowed_formats: dict[str, str] = field(
        default_factory=lambda: {
            ".jpeg": "JPEG",
            ".jpg": "JPEG",
            ".pdf": "PDF",
            ".png": "PNG",
            ".webp": "WEBP",
        }
    )
    max_document_bytes: int = 50 * 1024 * 1024
    max_fetch_documents: int = 20
    max_upload_redirects: int = 3
    # ZIPs are served as download links, so they are not bound by MCP message size.
    max_archive_documents: int = 200
    max_archive_bytes: int = 2 * 1024 * 1024 * 1024
    download_ttl_seconds: int = 24 * 60 * 60
    max_download_store_bytes: int = 10 * 1024 * 1024 * 1024
    upload_ttl_seconds: int = 15 * 60
    # A persistent volume, so download links keep working across restarts.
    download_dir: Path = Path("/data/downloads")
    display_max_edge: int = 1280
    # Page images sent to the model when even OCR finds no usable text.
    max_vision_pages: int = 3
    # Whether those page images also go beside the parsed text, to show the layout.
    page_images_with_text: bool = True
    # Text sent to the model; stored text (for search and reading) may be longer.
    max_prompt_chars: int = 24_000
    # Ollama context window per request; must hold the prompt, schema, and reply. Visia
    # and Fiora request the same, so the apps share one loaded copy of the model.
    context_tokens: int = 16_384
    # Semantic search: the Sentia embedding model, its vector size, and passage sizes.
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_dimensions: int = 1024
    chunk_chars: int = 1200
    chunk_overlap_chars: int = 200
    # Where Docling runs its layout, table, and OCR models: auto uses a GPU when present.
    docling_device: str = "auto"
    # Google Drive import: the UI's OAuth client and a key, derived from the API token,
    # that encrypts the stored refresh token.
    drive_client_id: str = ""
    drive_client_secret: str = ""
    drive_redirect_uri: str = ""
    token_encryption_key: str = ""
    # Files one Drive folder import or bulk upload may queue.
    max_import_items: int = 500
    max_stored_text_chars: int = 200_000
    max_read_chars: int = 40_000
    max_tags: int = 32
    search_limit_max: int = 100
    search_stop_words: frozenset[str] = frozenset(
        {
            "about",
            "all",
            "are",
            "count",
            "do",
            "doc",
            "docs",
            "document",
            "documents",
            "file",
            "files",
            "find",
            "for",
            "how",
            "many",
            "me",
            "my",
            "of",
            "show",
            "that",
            "the",
            "there",
            "what",
            "with",
        }
    )
    # Public HTTPS origin of this MCP server, used for its icon URLs ("" = embedded icons only).
    mcp_public_url: str = ""

    @staticmethod
    def google_client(path: Path) -> tuple[str, str]:
        """Read the Google client ID and secret from the UI's ``[auth]`` secrets, if mounted."""
        try:
            auth = tomllib.loads(path.read_text()).get("auth", {})
        except (OSError, tomllib.TOMLDecodeError):
            return "", ""
        return str(auth.get("client_id", "")).strip(), str(auth.get("client_secret", "")).strip()

    @staticmethod
    def derive_encryption_key(api_token: str) -> str:
        """A Fernet key derived from the API token; rotating the token means reconnecting Drive."""
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"lexia-drive-refresh-token").derive(
            api_token.encode()
        )
        return base64.urlsafe_b64encode(key).decode()

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Build and validate backend configuration from the environment."""
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("DATABASE_URL is required.")
        ollama_url = os.getenv("OLLAMA_URL", "http://luna-sentia-ollama:11434").rstrip("/")
        parsed = urlparse(ollama_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("OLLAMA_URL must be a valid HTTP(S) URL.")
        document_model = os.getenv("DOCUMENT_MODEL", "qwen3-vl:8b-instruct").strip()
        if not document_model:
            raise RuntimeError("DOCUMENT_MODEL cannot be blank.")
        public_base_url = os.getenv("LUNALEXIA_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if public_base_url:
            public_url = urlparse(public_base_url)
            if public_url.scheme != "https" or not public_url.netloc:
                raise RuntimeError("LUNALEXIA_PUBLIC_BASE_URL must be a valid HTTPS URL.")
        api_token = os.getenv("LUNALEXIA_API_TOKEN", "").strip()
        if len(api_token) < 32:
            raise RuntimeError("LUNALEXIA_API_TOKEN must contain at least 32 characters.")
        docling_device = os.getenv("DOCLING_DEVICE", "auto").strip().lower()
        if docling_device not in cls.DOCLING_DEVICES:
            raise RuntimeError(f"DOCLING_DEVICE must be one of: {', '.join(sorted(cls.DOCLING_DEVICES))}.")
        page_images_with_text = os.getenv("PAGE_IMAGES_WITH_TEXT", "true").strip().lower()
        if page_images_with_text not in {"true", "false"}:
            raise RuntimeError("PAGE_IMAGES_WITH_TEXT must be true or false.")
        mcp_public_url = os.getenv("MCP_PUBLIC_URL", "").strip().rstrip("/")
        if mcp_public_url:
            parsed_public_url = urlparse(mcp_public_url)
            local_http = (
                parsed_public_url.scheme == "http"
                and parsed_public_url.hostname in cls.LOCAL_HOSTS
            )
            if not (parsed_public_url.scheme == "https" or local_http) or not parsed_public_url.netloc:
                raise RuntimeError("MCP_PUBLIC_URL must be an HTTPS URL, or HTTP on localhost.")
        drive_client_id, drive_client_secret = cls.google_client(cls.GOOGLE_OAUTH_FILE)
        return cls(
            drive_client_id=drive_client_id,
            drive_client_secret=drive_client_secret,
            drive_redirect_uri=f"{mcp_public_url or 'http://localhost:18004'}/api/drive/callback",
            token_encryption_key=cls.derive_encryption_key(api_token),
            mcp_public_url=mcp_public_url,
            document_dir=Path(os.getenv("DOCUMENT_DIR", "/data/documents")),
            database_url=database_url,
            ollama_url=ollama_url,
            document_model=document_model,
            page_images_with_text=page_images_with_text == "true",
            embedding_model=os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b").strip(),
            embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "1024")),
            public_base_url=public_base_url,
            api_token=api_token,
            docling_device=docling_device,
        )
