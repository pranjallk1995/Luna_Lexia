"""MCP tools that let LLM clients manage Luna Lexia documents."""

from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar

import anyio
from mcp.server.fastmcp import FastMCP, Image as MCPImage
from pydantic import BaseModel, ConfigDict, WithJsonSchema

from app.core.models import MetadataChange, MetadataRules
from app.core.schemas import DocTypeName
from app.integrations.remote import DocumentSourceResolver
from app.services.application import LexiaService, PreviewResult

Result = TypeVar("Result")

# ChatGPT passes attachments to parameters listed here as {download_url, file_id}.
FILE_PARAMS_META = {"openai/fileParams": ["file"]}


class FileReference(BaseModel):
    """A client-hosted attachment, as passed by ChatGPT for file parameters."""

    model_config = ConfigDict(extra="allow")

    download_url: str
    file_id: str | None = None


# Inline (no $ref) so every LLM client can read the parameter shape.
FileInput = Annotated[
    FileReference | None,
    WithJsonSchema(
        {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "download_url": {"type": "string"},
                        "file_id": {"type": "string"},
                    },
                    "required": ["download_url"],
                },
                {"type": "null"},
            ],
            "description": "An attached document file passed by the chat client.",
        }
    ),
]


class MetadataUpdate(BaseModel):
    """One document's title, summary, tag, and extracted-field change within a batch."""

    name: str
    title: str | None = None
    summary: str | None = None
    add_tags: list[str] | None = None
    remove_tags: list[str] | None = None
    extracted: dict[str, Any] | None = None
    mark_reviewed: bool = False


class RenameRequest(BaseModel):
    """One document rename within a batch."""

    name: str
    new_name: str


TAG_LIST_SCHEMA = {"type": "array", "items": {"type": "string"}}

MetadataUpdates = Annotated[
    list[MetadataUpdate],
    WithJsonSchema(
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "add_tags": TAG_LIST_SCHEMA,
                    "remove_tags": TAG_LIST_SCHEMA,
                    "extracted": {"type": "object"},
                    "mark_reviewed": {"type": "boolean"},
                },
                "required": ["name"],
            },
            "description": "One entry per document: its exact name and the changes to apply.",
        }
    ),
]

RenameRequests = Annotated[
    list[RenameRequest],
    WithJsonSchema(
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "new_name": {"type": "string"}},
                "required": ["name", "new_name"],
            },
            "description": "One entry per document: its exact current name and the new name.",
        }
    ),
]

DocTypes = list[DocTypeName] | None


async def offload(function: Callable[..., Result], *args: Any) -> Result:
    """Run slow, blocking work (model analysis, rendering, archives) off the event loop.

    FastMCP calls synchronous tools on the event loop, so a blocking tool would
    stall every other request, including the web UI and health checks.
    """
    return await anyio.to_thread.run_sync(function, *args)


class LexiaMCPTools:
    """Register Luna Lexia's MCP tool surface on a FastMCP server.

    Hidden documents are never reachable through these public tools.
    """

    def __init__(
        self,
        server: FastMCP,
        service: LexiaService,
        sources: DocumentSourceResolver,
    ) -> None:
        self.server = server
        self.service = service
        self.sources = sources
        self._register_library_tools()
        self._register_delivery_tools()
        self._register_mutation_tools()

    @staticmethod
    def content(result: PreviewResult) -> list:
        """Return a rendered preview as image content plus a JSON key to it."""
        return [MCPImage(data=result.jpeg, format="jpeg"), result.summary()]

    def preview(self, names: list[str]) -> list:
        return self.content(self.service.previews.preview(names))

    async def with_preview(self, listing: dict[str, Any], items_key: str, include_preview: bool):
        """Return ``listing``, adding a sheet of its first documents' first pages on request."""
        names = [item["name"] for item in listing[items_key]][: self.service.previews.max_documents]
        if not include_preview or not names:
            return listing
        image, summary = await offload(self.preview, names)
        return [listing, image, {"preview": summary}]

    def _register_library_tools(self) -> None:
        server, visible, search, indexer, service = (
            self.server,
            self.service.visible,
            self.service.search,
            self.service.indexer,
            self.service,
        )

        @server.tool()
        async def list_documents(
            limit: int = 50,
            offset: int = 0,
            doc_types: DocTypes = None,
            financial_year: str | None = None,
            include_preview: bool = False,
        ):
            """List visible documents newest first, one page at a time.

            Each document has its exact name, doc_type, title, financial_year
            (Indian April-March year such as "2024-25"), document_date, issuer,
            primary_amount, tags, favorite flag, page count, size, and public_url.
            Filter by doc_types and/or financial_year. Follow next_offset for the
            next page (null on the last page). Set include_preview=true to also
            get a numbered sheet of the first pages of up to 20 documents.
            """
            listing = await offload(
                visible.list_page, limit, offset, search.limit_max, doc_types, financial_year
            )
            return await self.with_preview(listing, "documents", include_preview)

        @server.tool()
        async def search_documents(
            query: str = "",
            tags: list[str] | None = None,
            match_all_tags: bool = False,
            doc_types: DocTypes = None,
            financial_year: str | None = None,
            limit: int = 50,
            offset: int = 0,
            include_preview: bool = False,
        ):
            """Search visible documents by words, tags, type, and financial year.

            The query matches filenames, titles, issuers, summaries, tags, and the
            documents' own text (for example an employer, PAN, or policy number).
            Tags must be lowercase; every document carries its type tag (such as
            "payslip" or "form 16") and an "fy 2024-25" style year tag. Results
            include public_url; follow next_offset for more. Set
            include_preview=true for a sheet of the first 20 results' first pages.
            """
            MetadataRules.require_lowercase_tags(tags, "tags")
            results = await offload(
                search.search, query, tags, match_all_tags, limit, offset, doc_types, financial_year
            )
            return await self.with_preview(results, "results", include_preview)

        @server.tool()
        async def get_document(name: str) -> dict[str, Any]:
            """Return everything known about one visible document.

            Includes doc_type, title, summary, tags, key facts (financial_year,
            document_date, issuer, primary_amount), the full "extracted" details
            for its type (see list_document_types for each type's fields),
            review_flags (business rules the details break, such as net pay not
            matching gross minus deductions; also tagged "needs review"),
            extraction_error when extraction failed, the model's confidence, size,
            SHA-256, timestamps, public_url, and a "file" object with the format,
            page count, text-layer flag, and embedded PDF metadata.
            """
            return await offload(visible.details, name)

        @server.tool()
        async def list_document_data(
            limit: int = 50,
            offset: int = 0,
            names: list[str] | None = None,
            doc_types: DocTypes = None,
            financial_year: str | None = None,
        ) -> dict[str, Any]:
            """Return the extracted details of many visible documents at once.

            Use this to gather supporting data for tax filing or financial
            tracking, for example every payslip and Form 16 of financial_year
            "2024-25". Pages newest first (up to 100 per call; follow
            next_offset), or pass names for an exact batch, with missing or hidden
            names listed under "unavailable". Each item has the list_documents
            summary plus summary, extracted, review_flags, and extraction_error.
            Fields absent from a document are null: they were not printed, so do
            not assume 0. Treat details with review_flags as unconfirmed until the
            user checks them.
            """
            return await offload(
                visible.data_page, limit, offset, search.limit_max, names, doc_types, financial_year
            )

        @server.tool()
        async def read_document_text(
            name: str, first_page: int = 1, last_page: int | None = None
        ) -> dict[str, Any]:
            """Return a visible document's parsed text, page by page, as Markdown.

            The text comes from Docling: the PDF text layer, OCR of scanned
            pages, and tables as Markdown tables. Use it for details the
            extracted fields do not cover. Output is capped at about 40,000
            characters; continue from next_page when it is not null.
            """
            return await offload(visible.read_text, name, first_page, last_page, service.max_read_chars)

        @server.tool()
        def list_document_types() -> dict[str, Any]:
            """List every document type Luna Lexia recognizes and the JSON schema of
            the details it extracts for each, to know which fields to read or correct."""
            return {"document_types": service.document_types()}

        @server.tool()
        async def get_vault_stats(top_tags: int = 10) -> dict[str, Any]:
            """Return statistics for the visible library, computed from stored metadata.

            Includes total_documents, total_pages, favorite_count,
            needs_review_count, extraction_failed_count, counts by_doc_type and by_financial_year,
            total and average size, the largest document, a count per file format,
            unique_tag_count, the top_tags most used tags, and the oldest and
            newest updated_at. Hidden documents are not counted.
            """
            return await offload(visible.stats, top_tags)

        @server.tool()
        async def backfill_documents() -> dict[str, Any]:
            """Analyze files missing current metadata; idempotent by hash, model, and schema.

            Re-extracts documents analyzed by an older model or schema version
            and reports files that could not be analyzed under "failed".
            """
            return await offload(indexer.backfill)

    def _register_delivery_tools(self) -> None:
        server, visible, urls, downloads, previews = (
            self.server,
            self.service.visible,
            self.service.urls,
            self.service.downloads,
            self.service.previews,
        )

        @server.tool()
        async def view_document(name: str, page: int = 1):
            """Show one page of a visible document as an image, with its title and public_url.

            page is 1-based; the result includes page_count for paging through.
            """
            return await offload(lambda: self.content(previews.page(name, page)))

        @server.tool()
        def get_document_url(name: str) -> dict[str, str]:
            """Return the public HTTPS URL that opens one non-hidden stored document."""
            return {"name": name, "url": visible.public_url(name)}

        @server.tool()
        def set_public_base_url(public_base_url: str) -> dict[str, Any]:
            """Change the public HTTPS origin used by all Luna Lexia document URLs.

            Supply the full externally reachable base, including any path prefix,
            for example https://device.tailnet.ts.net/lexia. Every stored document
            URL is regenerated immediately; no service restart is required.
            """
            return urls.update(public_base_url)

        @server.tool()
        async def fetch_documents(names: list[str], delivery: Literal["display", "zip"] = "display"):
            """Show visible documents in chat, or package them as a downloadable ZIP.

            Use exact filenames returned by list_documents or search_documents.
            - display: the first page of one document, or a numbered sheet of first
              pages for up to 20 documents, plus each one's title and public_url.
              Names that are no longer available are listed under "unavailable".
            - zip: packages the original files on the server and returns a
              download_url (at most 200 documents, 2 GiB). Give the user that link
              exactly as returned; it expires at expires_at (24 hours) and the
              ZIP is not embedded in this result.
            Hidden documents are never available through this public tool.
            """
            if delivery == "zip":
                return await offload(downloads.create_zip, names)
            return await offload(self.preview, names)

    def _register_mutation_tools(self) -> None:
        server, visible, sources, uploads = (
            self.server,
            self.service.visible,
            self.sources,
            self.service.uploads,
        )

        def upload(
            name: str | None,
            base64_data=None,
            document_url=None,
            file=None,
            password=None,
            doc_type=None,
        ) -> dict[str, Any]:
            source = sources.resolve(base64_data, document_url, file.model_dump() if file else None)
            return visible.upload(name or source.suggested_name, source.data, password, doc_type)

        @server.tool(meta=FILE_PARAMS_META)
        async def upload_document(
            name: str | None = None,
            file: FileInput = None,
            document_url: str | None = None,
            content_base64: str | None = None,
            password: str | None = None,
            doc_type: DocTypeName | None = None,
        ) -> dict[str, Any]:
            """Add a PDF or a photo/scan (PNG, JPEG, WebP) to Luna Lexia.

            The document is parsed (with OCR for scans), classified (payslip,
            Form 16, bank statement...), and its type's details are extracted
            and checked; the result includes them and any review_flags. Provide
            exactly one source:
            - file: a document the user attached in chat (ChatGPT supplies it).
            - document_url: a public HTTPS URL or a data URL.
            - content_base64: Base64 bytes; only practical for small files.
            To upload a local file from a shell or code sandbox, use
            create_upload_link instead of Base64.

            password unlocks a protected PDF, which is then stored unlocked; ask
            the user for it if the upload reports that the PDF is protected.
            doc_type skips guessing when the type is known. name is optional: the
            extension is corrected to match the format, a taken name gets a
            numeric suffix, and an exact duplicate returns status "duplicate".
            Maximum size: 50 MiB. Analysis can take a minute.
            """
            return await offload(upload, name, content_base64, document_url, file, password, doc_type)

        @server.tool()
        async def add_document(
            name: str,
            content_base64: str,
            password: str | None = None,
            doc_type: DocTypeName | None = None,
        ) -> dict[str, Any]:
            """Add a small document from Base64. Prefer upload_document for attachments and URLs."""
            return await offload(upload, name, content_base64, None, None, password, doc_type)

        @server.tool()
        def create_upload_link(name: str, replace: bool = False) -> dict[str, Any]:
            """Create a single-use HTTPS link for uploading a local document file.

            Use this when you can run commands (a shell or code sandbox) and have
            the document as a file: POST its bytes to upload_url, for example with
            the returned curl_example. The link expires after 15 minutes. With
            replace=true, name must be an existing visible document to overwrite.
            """
            return uploads.create_link(name, replace)

        @server.tool(meta=FILE_PARAMS_META)
        async def replace_document(
            name: str,
            content_base64: str | None = None,
            document_url: str | None = None,
            file: FileInput = None,
            password: str | None = None,
            doc_type: DocTypeName | None = None,
        ) -> dict[str, Any]:
            """Re-analyze and atomically replace a non-hidden document and its details.

            Provide exactly one of file, document_url, or content_base64; the new
            file must have the same format as the existing file's extension.
            """

            def replace() -> dict[str, Any]:
                source = sources.resolve(content_base64, document_url, file.model_dump() if file else None)
                return {
                    "status": "replaced",
                    **visible.ingest_bytes(name, source.data, True, password, doc_type),
                }

            return await offload(replace)

        @server.tool()
        async def reextract_document(name: str, doc_type: DocTypeName | None = None) -> dict[str, Any]:
            """Classify a visible document again and re-extract its details.

            Pass doc_type when the stored type is wrong; the details are then
            extracted with that type's schema. Manual corrections are replaced.
            """
            return await offload(visible.reextract, name, doc_type)

        @server.tool()
        def update_document_metadata(
            name: str,
            title: str | None = None,
            summary: str | None = None,
            add_tags: list[str] | None = None,
            remove_tags: list[str] | None = None,
            extracted: dict[str, Any] | None = None,
            mark_reviewed: bool = False,
        ) -> dict[str, Any]:
            """Edit a visible document's title, summary, tags, or extracted fields.

            extracted holds corrected values for top-level fields of the
            document's type, for example {"net_pay": 85000, "pay_date":
            "2025-03-31"}; they are validated against the type's schema (see
            list_document_types), and the key facts and review_flags are
            recomputed. Set mark_reviewed=true only after the user confirms the
            details are correct; it clears review_flags and the "needs review"
            tag. Tags must be lowercase; a tag containing capital letters is
            rejected and nothing is changed.
            """
            MetadataRules.require_lowercase_tags(add_tags, "add_tags")
            MetadataRules.require_lowercase_tags(remove_tags, "remove_tags")
            change = MetadataChange(title, summary, add_tags, remove_tags, extracted, mark_reviewed)
            return {"status": "metadata_updated", **visible.update_metadata(name, change)}

        @server.tool()
        async def update_documents_metadata(updates: MetadataUpdates) -> dict[str, Any]:
            """Edit titles, summaries, tags, and extracted fields on up to 100 documents.

            Each entry is applied independently: successes are listed under
            "updated" and per-document errors under "failed" (for example a
            missing name or a field that does not belong to the document's type).
            Tags must be lowercase; any capitalized tag rejects the whole batch
            and nothing is changed.
            """
            for update in updates:
                MetadataRules.require_lowercase_tags(update.add_tags, f"add_tags for {update.name!r}")
                MetadataRules.require_lowercase_tags(update.remove_tags, f"remove_tags for {update.name!r}")
            changes = [
                (
                    update.name,
                    MetadataChange(
                        update.title,
                        update.summary,
                        update.add_tags,
                        update.remove_tags,
                        update.extracted,
                        update.mark_reviewed,
                    ),
                )
                for update in updates
            ]
            return {"status": "metadata_updated", **await offload(visible.update_metadata_many, changes)}

        @server.tool()
        async def rename_documents(renames: RenameRequests) -> dict[str, Any]:
            """Rename up to 100 visible documents at once, applied in the given order.

            The same rules as rename_document apply to each: only letters,
            numbers, and underscores, the extension unchanged, and the immutable
            ID and public URL kept. Each rename is independent: successes are
            listed under "renamed" and per-document errors under "failed".
            """
            pairs = [(rename.name, rename.new_name) for rename in renames]
            return {"status": "renamed", **await offload(visible.rename_many, pairs)}

        @server.tool()
        def rename_document(name: str, new_name: str) -> dict[str, Any]:
            """Rename a document using only letters, numbers, and underscores.

            Keep the existing extension. Spaces, hyphens, and other special
            characters are not allowed. The immutable ID and public URL remain
            unchanged.
            """
            return visible.rename(name, new_name)

        @server.tool()
        def set_favorite_documents(names: list[str], favorite: bool) -> dict[str, Any]:
            """Add or remove visible documents from Favorites.

            Use exact filenames returned by list_documents or search_documents.
            Set favorite to true to add the documents or false to remove them.
            """
            return visible.set_favorite(names, favorite)

        @server.tool()
        def remove_document(name: str) -> dict[str, str]:
            """Remove a non-hidden document and its persisted metadata consistently."""
            return visible.remove(name)
