"""Compose and run the Luna Lexia MCP and HTTP server."""

import uvicorn
from mcp.server.fastmcp import FastMCP

from app.core.config import AppConfig
from app.data.database import Database
from app.data.repositories import DocumentRepository, PublicURLRepository
from app.integrations.extraction import DocumentAnalyzer
from app.integrations.ollama import OllamaChat
from app.integrations.parsing import DoclingParser
from app.integrations.remote import DocumentSourceResolver
from app.services.application import LexiaService
from app.services.documents import DocumentReader
from app.transport.branding import ServerBranding
from app.transport.http_routes import LexiaHTTPRoutes
from app.transport.mcp_tools import LexiaMCPTools


class LunaLexiaApplication:
    """Compose and own the concrete Luna Lexia dependencies."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.database = Database(config.database_url)
        self.database.initialize_schema()
        reader = DocumentReader()
        self.service = LexiaService.create(
            config,
            DocumentRepository(self.database),
            PublicURLRepository(self.database),
            DoclingParser(reader, config.max_stored_text_chars, config.docling_device),
            DocumentAnalyzer(
                OllamaChat(config.ollama_url, config.document_model, config.context_tokens),
                reader,
                config.max_prompt_chars,
                config.max_vision_pages,
            ),
        )
        self.branding = ServerBranding(config.mcp_public_url, name="Luna Lexia")
        self.mcp = FastMCP(
            "Luna Lexia", host=config.host, port=config.port, icons=self.branding.mcp_icons()
        )
        LexiaMCPTools(
            self.mcp,
            self.service,
            DocumentSourceResolver(
                self.service.visible.decoder, config.max_document_bytes, config.max_upload_redirects
            ),
        )
        LexiaHTTPRoutes(self.mcp, self.service, self.branding, config.api_token)

    def run(self) -> None:
        """Serve Luna Lexia over the streamable HTTP transport."""
        uvicorn.run(self.mcp.streamable_http_app(), host=self.config.host, port=self.config.port)


application = LunaLexiaApplication(AppConfig.from_env())
mcp = application.mcp

if __name__ == "__main__":
    application.run()
