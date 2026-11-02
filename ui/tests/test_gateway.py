import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image
from starlette.testclient import TestClient

from config import SSOConfig, UIConfig
from gateway import UIGateway

OWNER = {"email": "owner@example.com", "email_verified": True}
STRANGER = {"email": "other@example.com", "email_verified": True}
TOKEN = "t" * 32


class GatewayTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.document_dir = Path(directory.name)
        Image.new("RGB", (64, 96), "white").save(self.document_dir / "a.png")
        Image.new("RGB", (64, 96), "white").save(self.document_dir / "doc.pdf", "PDF", save_all=True, append_images=[Image.new("RGB", (64, 96), "red")])
        self.backend_requests: list[httpx.Request] = []
        self.timeouts: list[float] = []

        def sso(request: httpx.Request) -> httpx.Response:
            session = {"owner": OWNER, "stranger": STRANGER}.get(request.headers["X-Luna-Session"])
            return httpx.Response(200, json=session) if session else httpx.Response(401)

        def backend(request: httpx.Request) -> httpx.Response:
            self.backend_requests.append(request)
            self.timeouts.append(request.extensions["timeout"]["read"])
            if request.url.path == "/api/metadata":
                documents = [{"name": "a.png", "size_bytes": 99}, {"name": "doc.pdf"}, {"name": "gone.pdf"}]
                return httpx.Response(200, json={"documents": documents})
            return httpx.Response(200, json={"status": "ok"})

        config = UIConfig(
            document_dir=self.document_dir,
            api_url="http://backend",
            api_token=TOKEN,
            allowed_emails=frozenset({"owner@example.com"}),
            sso=SSOConfig(public_url="https://auth.example.com"),
        )
        gateway = UIGateway(config, httpx.MockTransport(backend), httpx.MockTransport(sso))
        self.client = TestClient(gateway.application(), base_url="https://lexia.example.com")

    def sign_in(self, who: str = "owner") -> None:
        self.client.cookies.set(SSOConfig.COOKIE_NAME, who)


class AccessTests(GatewayTestCase):
    def test_every_data_route_needs_an_allowed_session(self):
        for path in ("/app/documents", "/app/hidden", "/app/thumbnails/a.png", "/app/pages/a.png/0", "/app/files/a.png", "/api/document-types"):
            self.client.cookies.clear()
            self.assertEqual(self.client.get(path).status_code, 401, path)
            self.sign_in("stranger")
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.backend_requests, [])

    def test_settings_reach_the_browser_but_never_the_token(self):
        self.sign_in()
        response = self.client.get("/auth/session")
        self.assertEqual(response.json()["settings"]["zoom_levels"], list(UIConfig.ZOOM_LEVELS))
        self.assertNotIn(TOKEN, response.text)


class FileTests(GatewayTestCase):
    def test_lists_only_documents_with_files(self):
        self.sign_in()
        documents = {document["name"]: document for document in self.client.get("/app/documents").json()["documents"]}
        self.assertEqual(set(documents), {"a.png", "doc.pdf"})
        self.assertEqual(documents["a.png"]["size_bytes"], 99)
        self.assertGreater(documents["doc.pdf"]["size_bytes"], 0)

    def test_thumbnail_of_a_pdf_and_an_image(self):
        self.sign_in()
        for name in ("a.png", "doc.pdf"):
            response = self.client.get(f"/app/thumbnails/{name}")
            self.assertEqual((response.status_code, response.headers["content-type"]), (200, "image/webp"), name)
            self.assertIn("immutable", response.headers["cache-control"])

    def test_pages_render_each_page_and_refuse_what_is_not_there(self):
        self.sign_in()
        first = self.client.get("/app/pages/doc.pdf/0")
        second = self.client.get("/app/pages/doc.pdf/1?size=zoom")
        self.assertEqual((first.status_code, first.headers["content-type"]), (200, "image/jpeg"))
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.content, second.content)
        self.assertEqual(self.client.get("/app/pages/doc.pdf/9").status_code, 404)
        self.assertEqual(self.client.get("/app/pages/a.png/1").status_code, 404)
        self.assertEqual(self.client.get("/app/pages/doc.pdf/0?size=huge").status_code, 400)

    def test_download_keeps_the_display_name_and_refuses_other_files(self):
        (self.document_dir / "secret.txt").write_text("x")
        self.sign_in()
        self.assertIn("slip.pdf", self.client.get("/app/files/doc.pdf?download=slip.pdf").headers["content-disposition"])
        self.assertEqual(self.client.get("/app/files/secret.txt").status_code, 404)
        self.assertEqual(self.client.get("/app/files/..%2F..%2Fetc%2Fpasswd").status_code, 404)


class ProxyTests(GatewayTestCase):
    def test_changes_the_ui_makes_are_forwarded(self):
        self.sign_in()
        for method, path in (
            ("POST", "/api/documents"),
            ("DELETE", "/api/documents/a%20b.pdf"),
            ("POST", "/api/documents/a.pdf/metadata"),
            ("POST", "/api/hidden/documents/a.pdf/reextract"),
            ("POST", "/api/favorites"),
            ("GET", "/api/drive/status"),
            ("POST", "/api/drive/disconnect"),
            ("POST", "/api/imports/drive-folder"),
            ("GET", "/api/imports/12345678-1234-1234-1234-123456789012"),
            ("POST", "/api/imports/12345678-1234-1234-1234-123456789012/retry"),
        ):
            self.assertEqual(self.client.request(method, path).status_code, 200, path)
        self.assertEqual(self.backend_requests[1].url.raw_path, b"/api/documents/a%20b.pdf")

    def test_query_reaches_the_backend(self):
        self.sign_in()
        self.client.get("/api/imports?limit=5")
        self.assertEqual(self.backend_requests[0].url.params["limit"], "5")

    def test_reading_a_document_may_take_long(self):
        self.sign_in()
        self.client.post("/api/documents/a.pdf/reextract")
        self.client.post("/api/documents")
        self.client.post("/api/imports/batch")
        self.client.post("/api/favorites")
        self.assertEqual(
            self.timeouts,
            [
                UIConfig.EXTRACT_TIMEOUT_SECONDS,
                UIConfig.EXTRACT_TIMEOUT_SECONDS,
                UIConfig.IMPORT_TIMEOUT_SECONDS,
                UIConfig.API_TIMEOUT_SECONDS,
            ],
        )

    def test_routes_the_ui_does_not_use_are_not_forwarded(self):
        self.sign_in()
        for method, path in (("GET", "/api/metadata"), ("GET", "/api/documents/a.pdf/text"), ("POST", "/api/reanalysis")):
            self.assertEqual(self.client.request(method, path).status_code, 404, path)
        self.assertEqual(self.backend_requests, [])


class ApplicationTests(GatewayTestCase):
    def test_health_needs_no_session_on_both_paths(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/_stcore/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
