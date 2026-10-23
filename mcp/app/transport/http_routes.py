"""REST routes used by the Luna Lexia Streamlit interface, peer apps, and public links.

Unlike public image links, document metadata holds PANs, salaries, and account
details, so every metadata and mutation route requires the private API token.
Only health, icons, public document links (unguessable IDs), and single-use
upload and download links are open.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app.core.models import MetadataChange
from app.services.application import LexiaService
from app.services.documents import display_name
from app.services.uploads import UploadLinkNotFound
from app.transport.auth import APIAuthenticator
from app.transport.branding import ServerBranding

Handler = Callable[[Request], Awaitable[Any]]


class LexiaHTTPRoutes:
    """Translate HTTP requests into service calls and results into JSON."""

    PUBLIC_DOCUMENT_HEADERS = {
        "Cache-Control": "private, max-age=300",
        "X-Content-Type-Options": "nosniff",
    }
    ICON_HEADERS = {
        "Cache-Control": "public, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    }

    def __init__(
        self,
        server: FastMCP,
        service: LexiaService,
        branding: ServerBranding,
        api_token: str,
    ) -> None:
        self.server = server
        self.service = service
        self.branding = branding
        self.authenticator = APIAuthenticator(api_token)
        self._register()

    @staticmethod
    def error(error: Exception) -> JSONResponse:
        status = 401 if isinstance(error, PermissionError) else 404 if isinstance(error, FileNotFoundError) else 400
        return JSONResponse({"error": str(error)}, status_code=status)

    @staticmethod
    async def payload(request: Request) -> dict[str, Any]:
        """Parse a JSON object request body."""
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object.")
        return value

    @classmethod
    async def metadata_change(cls, request: Request) -> MetadataChange:
        payload = await cls.payload(request)
        mark_reviewed = payload.get("mark_reviewed", False)
        if not isinstance(mark_reviewed, bool):
            raise ValueError("mark_reviewed must be true or false.")
        extracted = payload.get("extracted")
        if extracted is not None and not isinstance(extracted, dict):
            raise ValueError("extracted must be an object of field values.")
        return MetadataChange(
            title=payload.get("title"),
            summary=payload.get("summary"),
            add_tags=payload.get("add_tags"),
            remove_tags=payload.get("remove_tags"),
            extracted=extracted,
            mark_reviewed=mark_reviewed,
        )

    @staticmethod
    def name(request: Request) -> str:
        return request.path_params["name"]

    @staticmethod
    def filters(request: Request) -> tuple[list[str] | None, str | None]:
        """Read repeatable ``doc_type`` and a single ``financial_year`` query parameter."""
        doc_types = request.query_params.getlist("doc_type") or None
        return doc_types, request.query_params.get("financial_year") or None

    def route(
        self,
        method: str,
        path: str,
        handler: Handler,
        status: int = 200,
        *,
        protected: bool = True,
    ) -> None:
        """Register ``handler`` with uniform error translation and, by default, auth."""

        async def endpoint(request: Request) -> Response:
            try:
                if protected:
                    rejection = self.authenticator.rejection(request)
                    if rejection is not None:
                        return rejection
                result = await handler(request)
                if isinstance(result, Response):
                    return result
                return JSONResponse(result, status_code=status)
            except Exception as error:
                return self.error(error)

        self.server.custom_route(path, methods=[method])(endpoint)

    def _register(self) -> None:
        self.route("GET", "/health", self.health, protected=False)
        self._register_icons()
        self._register_documents()
        self._register_favorites()
        self._register_downloads()
        self._register_uploads()
        self._register_hidden_documents()

    async def health(self, request: Request) -> dict:
        return {"status": "ok", "service": "luna-lexia"}

    def _register_icons(self) -> None:
        branding = self.branding

        async def favicon(request: Request):
            return FileResponse(
                branding.favicon_path(), media_type="image/x-icon", headers=self.ICON_HEADERS
            )

        async def icon(request: Request):
            return FileResponse(
                branding.icon_path(self.name(request)),
                media_type="image/png",
                headers=self.ICON_HEADERS,
            )

        async def home(request: Request):
            return HTMLResponse(branding.home_page(), headers=self.ICON_HEADERS)

        self.route("GET", "/", home, protected=False)
        self.route("GET", "/favicon.ico", favicon, protected=False)
        self.route("GET", "/icons/{name}", icon, protected=False)

    def _register_documents(self) -> None:
        visible, service = self.service.visible, self.service

        async def metadata(request: Request):
            doc_types, financial_year = self.filters(request)
            return {"documents": await anyio.to_thread.run_sync(visible.list, doc_types, financial_year)}

        async def document_types(request: Request):
            return {"document_types": service.document_types()}

        async def details(request: Request):
            return await anyio.to_thread.run_sync(visible.details, self.name(request))

        async def text(request: Request):
            first = int(request.query_params.get("first_page", 1))
            last = request.query_params.get("last_page")
            return await anyio.to_thread.run_sync(
                visible.read_text,
                self.name(request),
                first,
                int(last) if last else None,
                service.max_read_chars,
            )

        async def public_document(request: Request):
            path = visible.public_path(self.name(request))
            return FileResponse(
                path,
                headers={
                    "Content-Disposition": f'inline; filename="{display_name(path.name)}"',
                    **self.PUBLIC_DOCUMENT_HEADERS,
                },
            )

        async def upload(request: Request):
            payload = await self.payload(request)
            created = await anyio.to_thread.run_sync(
                lambda: visible.ingest(
                    payload["name"],
                    payload["content_base64"],
                    password=payload.get("password") or None,
                    doc_type=payload.get("doc_type") or None,
                )
            )
            return {"status": "created", **created}

        async def delete(request: Request):
            return visible.remove(self.name(request))

        async def rename(request: Request):
            payload = await self.payload(request)
            return visible.rename(self.name(request), payload.get("new_name", ""))

        async def update_metadata(request: Request):
            change = await self.metadata_change(request)
            return {
                "status": "metadata_updated",
                **visible.update_metadata(self.name(request), change),
            }

        async def reextract(request: Request):
            payload = await self.payload(request)
            return await anyio.to_thread.run_sync(
                visible.reextract, self.name(request), payload.get("doc_type") or None
            )

        self.route("GET", "/api/metadata", metadata)
        self.route("GET", "/api/document-types", document_types)
        self.route("GET", "/api/documents/{name}", details)
        self.route("GET", "/api/documents/{name}/text", text)
        self.route("GET", "/api/public/documents/{name}", public_document, protected=False)
        self.route("POST", "/api/documents", upload, 201)
        self.route("DELETE", "/api/documents/{name}", delete)
        self.route("POST", "/api/documents/{name}/rename", rename)
        self.route("POST", "/api/documents/{name}/metadata", update_metadata)
        self.route("POST", "/api/documents/{name}/reextract", reextract)

    def _register_downloads(self) -> None:
        downloads = self.service.downloads

        async def download(request: Request):
            try:
                ticket, path = downloads.archive(request.path_params["token"])
            except FileNotFoundError as error:
                return JSONResponse({"error": str(error)}, status_code=404)
            return FileResponse(
                path,
                media_type="application/zip",
                filename=ticket.file_name,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )

        self.route("GET", "/api/downloads/{token}", download, protected=False)

    @staticmethod
    async def read_upload(request: Request, max_bytes: int) -> tuple[bytes, str | None, str | None]:
        """Read a raw or multipart (field ``file``) upload body, enforcing ``max_bytes``.

        A PDF password may come as a multipart field or a query parameter named
        ``password``; returns the bytes, the client's filename, and the password.
        """
        too_large = ValueError(f"Upload exceeds the {max_bytes // (1024 * 1024)} MiB limit.")
        password = request.query_params.get("password") or None
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            async with request.form(max_files=1) as form:
                upload = form.get("file")
                if upload is None or isinstance(upload, str):
                    raise ValueError("Send the document in a multipart field named 'file'.")
                data = await upload.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise too_large
                form_password = form.get("password")
                if isinstance(form_password, str) and form_password:
                    password = form_password
                return data, upload.filename, password
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > max_bytes:
                raise too_large
        return bytes(data), None, password

    def _register_uploads(self) -> None:
        uploads = self.service.uploads

        async def receive(request: Request):
            data, file_name, password = await self.read_upload(request, uploads.max_bytes)
            try:
                return await anyio.to_thread.run_sync(
                    uploads.receive, request.path_params["token"], data, file_name, password
                )
            except UploadLinkNotFound as error:
                return JSONResponse({"error": str(error)}, status_code=404)

        self.route("POST", "/api/uploads/{token}", receive, 201, protected=False)

    def _register_favorites(self) -> None:
        visible = self.service.visible

        async def favorites(request: Request):
            return {"documents": visible.favorites()}

        async def update_favorites(request: Request):
            payload = await self.payload(request)
            favorite = payload.get("favorite")
            if not isinstance(favorite, bool):
                raise ValueError("favorite must be true or false.")
            return visible.set_favorite(payload.get("names", []), favorite)

        self.route("GET", "/api/favorites", favorites)
        self.route("POST", "/api/favorites", update_favorites)

    def _register_hidden_documents(self) -> None:
        hidden = self.service.hidden

        async def documents(request: Request):
            return {"documents": hidden.list()}

        async def update_metadata(request: Request):
            change = await self.metadata_change(request)
            return {
                "status": "metadata_updated",
                **hidden.update_metadata(self.name(request), change),
            }

        async def reextract(request: Request):
            payload = await self.payload(request)
            return await anyio.to_thread.run_sync(
                hidden.reextract, self.name(request), payload.get("doc_type") or None
            )

        async def hide(request: Request):
            return hidden.set_hidden((await self.payload(request)).get("names", []), True)

        async def restore(request: Request):
            return hidden.set_hidden((await self.payload(request)).get("names", []), False)

        async def delete(request: Request):
            return hidden.remove_many((await self.payload(request)).get("names", []))

        self.route("GET", "/api/hidden/documents", documents)
        self.route("POST", "/api/hidden/documents/{name}/metadata", update_metadata)
        self.route("POST", "/api/hidden/documents/{name}/reextract", reextract)
        self.route("POST", "/api/hidden/hide", hide)
        self.route("POST", "/api/hidden/delete", delete)
        self.route("POST", "/api/hidden/restore", restore)
