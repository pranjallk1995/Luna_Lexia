import json
import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image
from starlette.testclient import TestClient

from config import SSOConfig, UIConfig
from gateway import UIGateway

OWNER = {"user_id": "owner", "email": "owner@example.com", "email_verified": True}
STRANGER = {"user_id": "stranger", "email": "other@example.com", "email_verified": True}
TOKEN = "t" * 32
PIN = "246810"


class GatewayTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.document_dir = Path(directory.name) / "u_owner"
        self.document_dir.mkdir()
        Image.new("RGB", (64, 96), "white").save(self.document_dir / "a.png")
        Image.new("RGB", (64, 96), "white").save(self.document_dir / "doc.pdf", "PDF", save_all=True, append_images=[Image.new("RGB", (64, 96), "red")])
        self.backend_requests: list[httpx.Request] = []
        self.timeouts: list[float] = []

        self.pin_set, self.sentia_up, self.hidden = True, True, set()
        self.sso_requests: list[httpx.Request] = []

        def sso(request: httpx.Request) -> httpx.Response:
            self.sso_requests.append(request)
            session = {"owner": OWNER, "owner-phone": OWNER, "stranger": STRANGER}.get(request.headers["X-Luna-Session"])
            if not session:
                return httpx.Response(401)
            if request.url.path == "/pin/status":
                return httpx.Response(200, json={"pin_set": self.pin_set, "locked_seconds": 0})
            if request.url.path == "/pin/verify":
                if not self.sentia_up:
                    return httpx.Response(503)
                if json.loads(request.content)["pin"] == PIN:
                    return httpx.Response(200, json={"ok": True})
                return httpx.Response(403, json={"error": "Wrong PIN. 4 tries left.", "attempts_left": 4})
            return httpx.Response(200, json=session)

        def backend(request: httpx.Request) -> httpx.Response:
            self.backend_requests.append(request)
            self.timeouts.append(request.extensions["timeout"]["read"])
            if request.url.path == "/api/hidden/documents":
                return httpx.Response(200, json={"documents": [{"name": name} for name in sorted(self.hidden)]})
            if request.url.path == "/api/metadata":
                documents = [{"name": "a.png", "size_bytes": 99}, {"name": "doc.pdf"}, {"name": "gone.pdf"}]
                return httpx.Response(200, json={"documents": documents})
            return httpx.Response(200, json={"status": "ok"})

        config = UIConfig(
            document_dir=self.document_dir.parent,
            api_url="http://backend",
            api_token=TOKEN,
            allowed_emails=frozenset({"owner@example.com"}),
            sso=SSOConfig(public_url="https://auth.example.com"),
        )
        self.gateway = gateway = UIGateway(config, httpx.MockTransport(backend), httpx.MockTransport(sso))
        self.client = TestClient(gateway.application(), base_url="https://lexia.example.com")

    def unlock(self, pin: str = None) -> httpx.Response:
        return self.client.post("/app/hidden/unlock", json={"pin": pin or PIN})

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
        self.unlock()
        for method, path in (
            ("POST", "/api/documents"),
            ("DELETE", "/api/documents/a%20b.pdf"),
            ("POST", "/api/documents/a.pdf/metadata"),
            ("POST", "/api/hidden/documents/a.pdf/reextract"),
            ("POST", "/api/favorites"),
            ("GET", "/api/drive/status"),
            ("POST", "/api/drive/disconnect"),
            ("POST", "/api/imports/drive-folder"),
            ("GET", "/api/gmail/status"),
            ("GET", "/api/gmail/authorization-url"),
            ("POST", "/api/gmail/disconnect"),
            ("POST", "/api/gmail/preview"),
            ("POST", "/api/imports/gmail"),
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



class HiddenPinTests(GatewayTestCase):
    """Hidden documents wait for the PIN that Luna Sentia checks."""

    FILE_ROUTES = (
            "/app/thumbnails/{name}",
            "/app/pages/{name}/0",
            "/app/files/{name}",
    )

    def expire_unlock(self):
        self.gateway.pin.unlocks = {key: (0.0, 0.0) for key in self.gateway.pin.unlocks}

    def test_the_hidden_tab_waits_for_the_right_pin(self):
        self.sign_in()
        self.assertEqual(self.client.get("/app/hidden").status_code, 423)
        status = self.client.get("/app/hidden/pin").json()
        self.assertEqual((status["pin_set"], status["unlocked"]), (True, False))
        self.assertEqual(status["settings_url"], "https://auth.example.com/pin?next=https%3A%2F%2Flexia.example.com%2F")
        wrong = self.unlock("111111")
        self.assertEqual((wrong.status_code, wrong.json()["attempts_left"]), (403, 4))
        self.assertEqual(self.client.get("/app/hidden").status_code, 423)
        self.assertEqual(self.unlock().json(), {"unlocked": True})
        self.assertEqual(self.client.get("/app/hidden").status_code, 200)
        self.assertTrue(self.client.get("/app/hidden/pin").json()["unlocked"])

    def test_locking_again_and_going_idle_close_it(self):
        self.sign_in()
        self.unlock()
        self.client.post("/app/hidden/lock")
        self.assertEqual(self.client.get("/app/hidden").status_code, 423)
        self.unlock()
        self.expire_unlock()
        self.assertEqual(self.client.get("/app/hidden").status_code, 423)

    def test_the_pin_is_checked_by_sentia_with_this_session(self):
        self.sign_in()
        self.unlock()
        verify = [request for request in self.sso_requests if request.url.path == "/pin/verify"][-1]
        self.assertEqual(verify.headers["X-Luna-Session"], "owner")
        self.assertEqual(str(verify.url), "http://luna-sentia-auth:8000/pin/verify")

    def test_unlocking_lasts_only_for_this_session(self):
        self.sign_in()
        self.unlock()
        self.sign_in("owner-phone")
        self.assertEqual(self.client.get("/app/hidden").status_code, 423)

    def test_without_a_pin_the_tab_asks_for_one_to_be_set(self):
        self.pin_set = False
        self.sign_in()
        status = self.client.get("/app/hidden/pin").json()
        self.assertEqual((status["pin_set"], status["unlocked"]), (False, False))
        self.assertEqual(self.client.get("/app/hidden").status_code, 423)

    def test_sentia_being_away_is_reported_and_unlocks_nothing(self):
        self.sentia_up = False
        self.sign_in()
        self.assertEqual(self.unlock().status_code, 409)
        self.assertEqual(self.client.get("/app/hidden").status_code, 423)

    def test_hidden_files_wait_for_the_pin_and_are_never_cached(self):
        self.hidden = {"a.png"}
        self.sign_in()
        for route in self.FILE_ROUTES:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route.format(name="a.png")).status_code, 404)
                self.assertNotEqual(self.client.get(route.format(name="doc.pdf")).status_code, 404)
        self.unlock()
        for route in self.FILE_ROUTES:
            with self.subTest(route=route, unlocked=True):
                answer = self.client.get(route.format(name="a.png"))
                self.assertEqual(answer.status_code, 200)
                self.assertEqual(answer.headers["Cache-Control"], "private, no-store")

    def test_hiding_an_item_locks_its_files_straight_away(self):
        self.sign_in()
        route = self.FILE_ROUTES[-1].format(name="a.png")
        self.assertEqual(self.client.get(route).status_code, 200)
        self.hidden = {"a.png"}
        self.assertEqual(self.client.post("/api/hidden/hide", json={"names": ["a.png"]}).status_code, 200)
        self.assertEqual(self.client.get(route).status_code, 404)

    def test_changing_hidden_items_waits_for_the_pin_but_hiding_does_not(self):
        self.sign_in()
        self.assertEqual(self.client.post("/api/hidden/restore", json={"names": ["a.png"]}).status_code, 423)
        self.assertEqual(self.client.post("/api/hidden/delete", json={"names": ["a.png"]}).status_code, 423)
        self.assertEqual(self.client.post("/api/hidden/hide", json={"names": ["a.png"]}).status_code, 200)
        self.unlock()
        self.assertEqual(self.client.post("/api/hidden/restore", json={"names": ["a.png"]}).status_code, 200)

    def test_files_are_refused_when_hidden_items_cannot_be_checked(self):
        self.sign_in()
        self.gateway.pin.forget_names("owner")
        original = self.gateway.backend._transport
        self.gateway.backend._transport = httpx.MockTransport(lambda request: httpx.Response(503))
        self.addCleanup(setattr, self.gateway.backend, "_transport", original)
        self.assertEqual(self.client.get(self.FILE_ROUTES[-1].format(name="doc.pdf")).status_code, 502)


if __name__ == "__main__":
    unittest.main()
