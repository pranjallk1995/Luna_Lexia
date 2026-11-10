"""Compose and run the Luna Lexia MCP and HTTP server."""

import uvicorn

from app.core import tenancy
from app.core.config import AppConfig
from app.data.database import Database
from app.data.tenants import adopt_loose_files, remove_folder
from app.transport.luna_mcp import LunaMCP
from app.data.repositories import ChunkRepository, DocumentRepository, ImportRepository, PublicURLRepository
from app.integrations.drive import DriveOAuth
from app.integrations.gmail import GmailOAuth
from app.integrations.embeddings import OllamaEmbedder
from app.integrations.extraction import DocumentAnalyzer, PasswordFinder
from app.integrations.ollama import OllamaChat
from app.integrations.parsing import IdleDoclingParser
from app.integrations.remote import DocumentSourceResolver
from app.services.application import LexiaService
from app.services.documents import DocumentReader
from app.services.imports import FileImporter, ImportJobs, ImportService
from app.services.semantic import Chunker, SemanticIndex
from app.services.showcase import ShowcaseSorter
from app.transport.branding import ServerBranding
from app.transport.http_routes import LexiaHTTPRoutes
from app.transport.mcp_tools import LexiaMCPTools


class LunaLexiaApplication:
    """Compose and own the concrete Luna Lexia dependencies."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.database = Database(config.database_url)
        self.database.initialize_schema()
        self.database.initialize_semantic_schema(config.embedding_dimensions)
        reader = DocumentReader()
        adopt_loose_files(config.document_dir)
        with tenancy.acting_as(tenancy.OWNER):
            self.service = LexiaService.create(
                config,
                DocumentRepository(self.database),
                PublicURLRepository(self.database),
                IdleDoclingParser(reader, config.max_stored_text_chars, config.docling_device),
                DocumentAnalyzer(
                    OllamaChat(config.ollama_url, config.document_model, config.context_tokens),
                    reader,
                    config.max_prompt_chars,
                    config.max_vision_pages,
                    config.page_images_with_text,
                ),
            )
        # Public links are served by this backend, so a new person's start at its address.
        self.database.tenants.after_create = lambda: self.service.urls.seed(config.mcp_public_url or config.public_base_url)
        sources = DocumentSourceResolver(
            self.service.visible.decoder, config.max_document_bytes, config.max_upload_redirects
        )
        imports = ImportRepository(self.database)
        password_finder = PasswordFinder(OllamaChat(config.ollama_url, config.document_model, config.context_tokens))
        jobs = ImportJobs(imports, FileImporter(self.service.visible, sources, imports, password_finder))
        self.service.imports = ImportService(
            imports,
            DriveOAuth(config.google_client_id, config.google_client_secret, config.drive_redirect_uri, config.api_token),
            jobs,
            config.token_encryption_key,
            config.max_import_items,
            gmail_oauth=GmailOAuth(config.google_client_id, config.google_client_secret, config.gmail_redirect_uri, config.api_token),
        )
        for user_id in self.database.tenants.users():
            with tenancy.acting_as(user_id):
                jobs.resume_unfinished()
        self.service.semantic = SemanticIndex(
            ChunkRepository(self.database),
            OllamaEmbedder(config.ollama_url, config.embedding_model, config.embedding_dimensions),
            Chunker(config.chunk_chars, config.chunk_overlap_chars),
            config.ollama_url,
            config.document_model,
            config.context_tokens,
        )
        self.service.showcase = ShowcaseSorter(self.service.visible.documents, self.service.semantic.tag)
        self.service.semantic.showcase = self.service.showcase
        self.service.hidden.showcase = self.service.showcase
        self.service.semantic.start()
        self.branding = ServerBranding(config.mcp_public_url, name="Luna Lexia")
        self.mcp = LunaMCP(
            "Luna Lexia",
            app="lexia",
            remove_user=self.remove_user,
            public_url=config.mcp_public_url or f"http://localhost:{config.port}",
            api_token=config.api_token,
            open_paths=("/api/public/", "/api/downloads/", "/api/uploads/", "/api/drive/callback", "/api/gmail/callback"),
            host=config.host,
            port=config.port,
            icons=self.branding.mcp_icons(),
        )
        LexiaMCPTools(self.mcp, self.service, sources)
        LexiaHTTPRoutes(self.mcp, self.service, self.branding, config.api_token)

    def remove_user(self, user_id: str) -> None:
        """Delete everything this person has in this app."""
        self.database.tenants.remove(user_id)
        remove_folder(self.config.document_dir, user_id)
        self.service.downloads.store.remove_user(user_id)
        self.service.reanalysis.forget(user_id)
        self.service.semantic.forget(user_id)


    def run(self) -> None:
        """Serve Luna Lexia over the streamable HTTP transport."""
        uvicorn.run(self.mcp.streamable_http_app(), host=self.config.host, port=self.config.port)


application = LunaLexiaApplication(AppConfig.from_env())
mcp = application.mcp

if __name__ == "__main__":
    application.run()
