"""Validated runtime configuration for the Luna Lexia backend."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse


@dataclass(frozen=True)
class AppConfig:
    """Immutable backend settings loaded from environment variables."""

    LOCAL_HOSTS: ClassVar[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})

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
    # Text sent to the model; stored text (for search and reading) may be longer.
    max_prompt_chars: int = 24_000
    # Ollama context window per request; must hold the prompt, schema, and reply.
    context_tokens: int = 16_384
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
        document_model = os.getenv("DOCUMENT_MODEL", "qwen2.5vl:7b").strip()
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
        mcp_public_url = os.getenv("MCP_PUBLIC_URL", "").strip().rstrip("/")
        if mcp_public_url:
            parsed_public_url = urlparse(mcp_public_url)
            local_http = (
                parsed_public_url.scheme == "http"
                and parsed_public_url.hostname in cls.LOCAL_HOSTS
            )
            if not (parsed_public_url.scheme == "https" or local_http) or not parsed_public_url.netloc:
                raise RuntimeError("MCP_PUBLIC_URL must be an HTTPS URL, or HTTP on localhost.")
        return cls(
            mcp_public_url=mcp_public_url,
            document_dir=Path(os.getenv("DOCUMENT_DIR", "/data/documents")),
            database_url=database_url,
            ollama_url=ollama_url,
            document_model=document_model,
            public_base_url=public_base_url,
            api_token=api_token,
        )
