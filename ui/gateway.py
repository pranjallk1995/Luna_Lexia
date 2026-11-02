"""The UI gateway: serves the React app, enforces sign-in, and fronts the backend.

The browser never sees the backend's API token. Every data route checks the
shared Luna session and this app's allowlist first, and only the routes the UI
uses are forwarded.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any
from urllib.parse import quote

import anyio
import httpx
from PIL import UnidentifiedImageError
from pypdfium2 import PdfiumError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from config import SSOConfig, UIConfig
from documents import DocumentFiles
from sso import LunaSingleSignOn

NAME = r"[^/]+"
JOB = r"[0-9a-fA-F-]+"
# The backend routes the browser app may reach, as (method, path pattern).
PROXIED_ROUTES = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"/api/document-types"),
        ("POST", r"/api/documents"),
        ("DELETE", rf"/api/documents/{NAME}"),
        ("POST", rf"/api/documents/{NAME}/(rename|metadata|reextract)"),
        ("POST", r"/api/favorites"),
        ("POST", r"/api/hidden/(hide|restore|delete)"),
        ("POST", rf"/api/hidden/documents/{NAME}/(metadata|reextract)"),
        ("GET", r"/api/drive/(status|authorization-url|folders)"),
        ("POST", r"/api/drive/disconnect"),
        ("GET", r"/api/imports"),
        ("GET", rf"/api/imports/{JOB}"),
        ("POST", r"/api/imports/(drive-folder|batch)"),
        ("POST", rf"/api/imports/{JOB}/retry"),
    )
)
EXTRACTING = re.compile(r"/api/documents|.*/reextract")
IMPORTING = re.compile(r"/api/imports/.*")
UNAVAILABLE = "Luna Lexia's backend is unavailable. Please try again."


UNREADABLE = (OSError, IndexError, UnidentifiedImageError, PdfiumError)


class StaticApp(StaticFiles):
    """Serve the built app: hashed bundles are cached forever, the page itself never."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        immutable = path.startswith("assets/")
        response.headers["Cache-Control"] = UIConfig.IMMUTABLE_CACHE_CONTROL if immutable else "no-cache"
        return response


class UIGateway:
    """Translate browser requests into session checks, vault reads, and backend calls."""

    def __init__(
        self,
        config: UIConfig,
        backend_transport: httpx.AsyncBaseTransport | None = None,
        sso_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.files = DocumentFiles(config.document_dir)
        self.sso = LunaSingleSignOn(config.allowed_emails, config.sso, sso_transport)
        self.backend = httpx.AsyncClient(
            base_url=config.api_url,
            headers={"Authorization": f"Bearer {config.api_token}"},
            timeout=UIConfig.API_TIMEOUT_SECONDS,
            transport=backend_transport,
        )

    # Sign-in

    @staticmethod
    def here(request: Request) -> str:
        """This app's public address. Behind a TLS-terminating proxy such as a Cloudflare
        tunnel, the proxy's X-Forwarded-Proto and X-Forwarded-Host give the real one."""
        headers = request.headers
        scheme = headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
        host = headers.get("x-forwarded-host", "").split(",")[0].strip() or request.url.netloc
        return f"{scheme}://{host}/"

    async def person(self, request: Request) -> dict[str, Any] | None:
        return await self.sso.user(request.cookies.get(SSOConfig.COOKIE_NAME))

    def protected(self, handler: Callable[[Request], Awaitable[Response]]):
        """Run ``handler`` only for a signed-in person on this app's allowlist."""

        async def endpoint(request: Request) -> Response:
            user = await self.person(request)
            if user is None:
                return JSONResponse({"error": "Sign in to continue."}, status_code=401)
            if not self.sso.is_allowed(user):
                return JSONResponse({"error": "This account is not authorized."}, status_code=403)
            return await handler(request)

        return endpoint

    async def session(self, request: Request) -> Response:
        """Tell the browser app who is signed in, with its settings once they are allowed."""
        here = self.here(request)
        user = await self.person(request)
        if user is None:
            return JSONResponse({"status": "signed_out", "login_url": self.sso.login_url(here)})
        logout_url = self.sso.logout_url(here)
        if not self.sso.is_allowed(user):
            return JSONResponse({"status": "forbidden", "email": user.get("email"), "logout_url": logout_url})
        return JSONResponse(
            {
                "status": "signed_in",
                "email": user.get("email"),
                "logout_url": logout_url,
                "settings": {
                    "documents_per_page": UIConfig.DOCUMENTS_PER_PAGE,
                    "max_tags": UIConfig.MAX_TAGS,
                    "name_max_chars": UIConfig.NAME_MAX_CHARS,
                    "title_max_chars": UIConfig.TITLE_MAX_CHARS,
                    "summary_max_chars": UIConfig.SUMMARY_MAX_CHARS,
                    "upload_types": UIConfig.UPLOAD_TYPES,
                    "zoom_levels": UIConfig.ZOOM_LEVELS,
                    "drive_refresh_seconds": UIConfig.DRIVE_REFRESH_SECONDS,
                    "recent_imports": UIConfig.RECENT_IMPORTS,
                },
            }
        )

    # Vault

    def collection(self, backend_path: str):
        async def endpoint(request: Request) -> Response:
            try:
                response = await self.backend.get(backend_path)
                response.raise_for_status()
            except httpx.HTTPError:
                return JSONResponse({"error": UNAVAILABLE}, status_code=502)
            records = await anyio.to_thread.run_sync(self.files.collection, response.json()["documents"])
            return JSONResponse({"documents": records}, headers={"Cache-Control": "no-store"})

        return endpoint

    async def thumbnail(self, request: Request) -> Response:
        name = request.path_params["name"]
        try:
            data = await anyio.to_thread.run_sync(self.files.thumbnail, name)
        except UNREADABLE:
            return JSONResponse({"error": "Document not found."}, status_code=404)
        return Response(data, media_type="image/webp", headers={"Cache-Control": UIConfig.IMMUTABLE_CACHE_CONTROL})

    async def page(self, request: Request) -> Response:
        """One page as a picture: ``?size=preview`` to read, or ``zoom`` for small print."""
        size = request.query_params.get("size", "preview")
        if size not in UIConfig.PAGE_EDGES:
            return JSONResponse({"error": "Unknown page size."}, status_code=400)
        name, index = request.path_params["name"], request.path_params["index"]
        try:
            data = await anyio.to_thread.run_sync(self.files.page, name, index, size)
        except UNREADABLE:
            return JSONResponse({"error": "Page not found."}, status_code=404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": UIConfig.IMMUTABLE_CACHE_CONTROL})

    async def file(self, request: Request) -> Response:
        """The original document, saved under ``?download=<file name>``."""
        try:
            path = self.files.path(request.path_params["name"])
        except FileNotFoundError:
            return JSONResponse({"error": "Document not found."}, status_code=404)
        download = request.query_params.get("download")
        serve = partial(FileResponse, path, headers={"Cache-Control": UIConfig.IMMUTABLE_CACHE_CONTROL})
        return serve(filename=download) if download else serve()

    # Backend

    @staticmethod
    def timeout(method: str, path: str) -> float:
        if method == "POST" and EXTRACTING.fullmatch(path):
            return UIConfig.EXTRACT_TIMEOUT_SECONDS
        if method == "POST" and IMPORTING.fullmatch(path):
            return UIConfig.IMPORT_TIMEOUT_SECONDS
        return UIConfig.API_TIMEOUT_SECONDS

    async def proxy(self, request: Request) -> Response:
        path = request.url.path
        if not any(method == request.method and pattern.fullmatch(path) for method, pattern in PROXIED_ROUTES):
            return JSONResponse({"error": "Not found."}, status_code=404)
        body = await request.body()
        if len(body) > UIConfig.MAX_REQUEST_BYTES:
            return JSONResponse({"error": "This request is too large."}, status_code=413)
        try:
            response = await self.backend.request(
                request.method,
                "/".join(quote(part, safe="") for part in path.split("/")),
                params=request.query_params,
                content=body,
                headers={"Content-Type": request.headers.get("content-type", "application/json")},
                timeout=self.timeout(request.method, path),
            )
        except httpx.HTTPError:
            return JSONResponse({"error": UNAVAILABLE}, status_code=502)
        return Response(
            response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
        )

    # Application

    @staticmethod
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    def application(self) -> Starlette:
        app = Starlette(
            routes=[
                Route("/health", self.health),
                # Luna Unia's status page checks every app's UI at Streamlit's health path.
                Route("/_stcore/health", self.health),
                Route("/auth/session", self.session),
                Route("/app/documents", self.protected(self.collection("/api/metadata"))),
                Route("/app/hidden", self.protected(self.collection("/api/hidden/documents"))),
                Route("/app/thumbnails/{name}", self.protected(self.thumbnail)),
                Route("/app/pages/{name}/{index:int}", self.protected(self.page)),
                Route("/app/files/{name}", self.protected(self.file)),
                Route("/api/{path:path}", self.protected(self.proxy), methods=["POST", "DELETE", "GET"]),
                Mount("/", StaticApp(directory=UIConfig.STATIC_DIR, html=True, check_dir=False)),
            ]
        )

        async def secure_headers(request: Request, call_next) -> Response:
            response = await call_next(request)
            response.headers["Content-Security-Policy"] = UIConfig.CONTENT_SECURITY_POLICY
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        app.add_middleware(BaseHTTPMiddleware, dispatch=secure_headers)
        return app


def create_app() -> Starlette:
    return UIGateway(UIConfig.from_env()).application()
