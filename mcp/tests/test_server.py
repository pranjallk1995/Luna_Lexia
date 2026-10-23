"""Tests for the MCP tool surface, HTTP routes, and document delivery helpers."""

import asyncio
import base64
import json
import tempfile
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image as PillowImage
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from fakes import (
    FakeAnalyzer,
    FakeDocumentRepository,
    FakeParser,
    FakePublicURLRepository,
    image_bytes,
    minimal_pdf,
    payslip_pdf,
    pdf_payload,
)

from app.core.config import AppConfig
from app.integrations.remote import DocumentSourceResolver
from app.services.application import LexiaService
from app.services.documents import DocumentArchive, DocumentReader, PreviewRenderer, display_name
from app.transport.branding import ServerBranding
from app.transport.http_routes import LexiaHTTPRoutes
from app.transport.mcp_tools import LexiaMCPTools

TOKEN = "x" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class ServerTestCase(unittest.TestCase):
    """Register the real adapters over an in-memory service graph."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.downloads_temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig(
            document_dir=self.root,
            database_url="postgresql://test",
            ollama_url="http://ollama:11434",
            document_model="test-model",
            public_base_url="https://lexia.example.test/vault",
            download_dir=Path(self.downloads_temp.name),
        )
        self.repository = FakeDocumentRepository()
        self.service = LexiaService.create(
            self.config,
            self.repository,
            FakePublicURLRepository(self.repository),
            FakeParser(),
            FakeAnalyzer(),
        )
        self.branding = ServerBranding()
        # Match production: a non-localhost bind leaves DNS-rebinding checks off for TestClient.
        self.server = FastMCP("Luna Lexia test", host="0.0.0.0", icons=self.branding.mcp_icons())
        self.mcp_tools = LexiaMCPTools(
            self.server,
            self.service,
            DocumentSourceResolver(
                self.service.visible.decoder,
                self.config.max_document_bytes,
                self.config.max_upload_redirects,
            ),
        )
        LexiaHTTPRoutes(self.server, self.service, self.branding, TOKEN)
        self.service.visible.ingest("one.pdf", pdf_payload("one"))
        self.service.visible.ingest("two.pdf", pdf_payload("two"), doc_type="form_16")

    def tearDown(self) -> None:
        self.temp.cleanup()
        self.downloads_temp.cleanup()

    def call(self, tool_name: str, arguments: dict):
        tool = self.server._tool_manager.get_tool(tool_name)
        return asyncio.run(tool.run(arguments))

    def client(self) -> TestClient:
        return TestClient(self.server.streamable_http_app())


class MCPToolTests(ServerTestCase):
    def test_upload_document_accepts_base64_with_type(self) -> None:
        result = self.call("upload_document", {"name": "april.pdf", "content_base64": pdf_payload("april"), "doc_type": "payslip"})
        self.assertEqual((result["status"], result["name"]), ("created", "april.pdf"))
        self.assertEqual(result["extracted"]["net_pay"], 80000.0)

    def test_upload_source_requires_exactly_one_input(self) -> None:
        resolver = self.mcp_tools.sources
        with self.assertRaisesRegex(ValueError, "exactly one"):
            resolver.resolve(None, None)
        with self.assertRaisesRegex(ValueError, "public"):
            resolver.resolve(None, "https://127.0.0.1/form16.pdf")

    def test_doc_type_parameters_are_enumerated(self) -> None:
        tool = next(t for t in asyncio.run(self.server.list_tools()) if t.name == "list_documents")
        self.assertIn("form_16", json.dumps(tool.inputSchema["properties"]["doc_types"]))
        with self.assertRaises(ToolError):
            self.call("list_documents", {"doc_types": ["receipt"]})

    def test_list_documents_filters_and_pages(self) -> None:
        listing = self.call("list_documents", {"doc_types": ["payslip"], "financial_year": "2024-25"})
        self.assertEqual([item["name"] for item in listing["documents"]], ["one.pdf"])
        self.assertNotIn("extracted", listing["documents"][0])
        first = self.call("list_documents", {"limit": 1})
        self.assertEqual((first["count"], first["next_offset"]), (2, 1))

    def test_list_document_data_returns_extracted_details(self) -> None:
        result = self.call("list_document_data", {"names": ["one.pdf", "missing.pdf"]})
        [item] = result["documents"]
        self.assertEqual(item["extracted"]["employer_name"], "Acme Corp")
        self.assertEqual(result["unavailable"], ["missing.pdf"])

    def test_get_document_and_text(self) -> None:
        document = self.call("get_document", {"name": "one.pdf"})
        self.assertEqual(document["file"]["page_count"], 1)
        text = self.call("read_document_text", {"name": "one.pdf"})
        self.assertIn("Net Pay", text["pages"][0]["text"])

    def test_list_document_types_describes_fields(self) -> None:
        types = {item["doc_type"]: item for item in self.call("list_document_types", {})["document_types"]}
        self.assertIn("net_pay", types["payslip"]["fields"]["properties"])

    def test_update_document_metadata_corrects_fields_and_marks_reviewed(self) -> None:
        corrected = self.call("update_document_metadata", {"name": "one.pdf", "extracted": {"net_pay": 1}})
        self.assertTrue(corrected["review_flags"])
        reviewed = self.call("update_document_metadata", {"name": "one.pdf", "mark_reviewed": True})
        self.assertEqual(reviewed["review_flags"], [])
        with self.assertRaisesRegex(ToolError, "lowercase"):
            self.call("update_document_metadata", {"name": "one.pdf", "add_tags": ["Tax"]})

    def test_batch_tools_report_failures_and_have_inline_schemas(self) -> None:
        result = self.call(
            "update_documents_metadata",
            {"updates": [{"name": "one.pdf", "add_tags": ["tax"]}, {"name": "two.pdf", "extracted": {"net_pay": 1}}]},
        )
        self.assertEqual((result["updated_count"], result["failed_count"]), (1, 1))
        self.assertIn("not form_16 fields", result["failed"][0]["error"])
        for name in ("update_documents_metadata", "rename_documents"):
            self.assertNotIn("$ref", json.dumps(self.server._tool_manager.get_tool(name).parameters))

    def test_rename_documents_applies_in_order(self) -> None:
        result = self.call(
            "rename_documents",
            {"renames": [{"name": "one.pdf", "new_name": "first.pdf"}, {"name": "two.pdf", "new_name": "one.pdf"}]},
        )
        self.assertEqual([item["name"] for item in result["renamed"]], ["first.pdf", "one.pdf"])
        self.assertTrue(result["renamed"][0]["public_url"].endswith("/doc-1"))

    def test_view_document_renders_requested_page(self) -> None:
        self.service.visible.ingest("long.pdf", base64.b64encode(minimal_pdf(["page"], pages=3)).decode())
        image, summary = self.call("view_document", {"name": "long.pdf", "page": 2})
        self.assertEqual(image.to_image_content().mimeType, "image/jpeg")
        self.assertEqual((summary["page"], summary["page_count"]), (2, 3))
        with self.assertRaises(ToolError):
            self.call("view_document", {"name": "long.pdf", "page": 4})

    def test_fetch_documents_display_skips_unavailable(self) -> None:
        self.service.hidden.set_hidden(["two.pdf"], True)
        image, summary = self.call("fetch_documents", {"names": ["one.pdf", "gone.pdf", "two.pdf"]})
        self.assertEqual([item["name"] for item in summary["previewed"]], ["one.pdf"])
        self.assertEqual(summary["unavailable"], ["gone.pdf", "two.pdf"])
        self.assertTrue(image.to_image_content().data)

    def test_fetch_documents_zip_returns_link(self) -> None:
        result = self.call("fetch_documents", {"names": ["one.pdf", "two.pdf"], "delivery": "zip"})
        self.assertEqual((result["status"], result["document_count"]), ("zip_ready", 2))
        self.assertTrue(result["download_url"].startswith("https://lexia.example.test/vault/api/downloads/"))

    def test_search_and_stats(self) -> None:
        results = self.call("search_documents", {"query": "acme", "doc_types": ["form_16"]})
        self.assertEqual([item["name"] for item in results["results"]], ["two.pdf"])
        listing, image, preview = self.call("search_documents", {"tags": ["payslip"], "include_preview": True})
        self.assertEqual(len(preview["preview"]["previewed"]), listing["count"])
        stats = self.call("get_vault_stats", {})
        self.assertEqual(stats["by_doc_type"], {"payslip": 1, "form_16": 1})

    def test_hidden_documents_are_unreachable(self) -> None:
        self.service.hidden.set_hidden(["one.pdf"], True)
        for tool, arguments in (
            ("get_document", {"name": "one.pdf"}),
            ("read_document_text", {"name": "one.pdf"}),
            ("view_document", {"name": "one.pdf"}),
            ("fetch_documents", {"names": ["one.pdf"], "delivery": "zip"}),
        ):
            with self.subTest(tool), self.assertRaises(ToolError):
                self.call(tool, arguments)

    def test_upload_tool_declares_chatgpt_file_param(self) -> None:
        tool = next(t for t in asyncio.run(self.server.list_tools()) if t.name == "upload_document")
        self.assertEqual(tool.meta, {"openai/fileParams": ["file"]})

    def test_upload_from_file_reference_uses_its_name(self) -> None:
        self.mcp_tools.sources._download = lambda url: (payslip_pdf("chat"), url)
        result = self.call(
            "upload_document",
            {"file": {"download_url": "https://files.example.test/abc", "file_name": "Form16 FY25.pdf"}},
        )
        self.assertEqual(result["name"], "Form16 FY25.pdf")

    def test_slow_upload_does_not_block_event_loop(self) -> None:
        analyzer = self.service.indexer.analyzer
        original = analyzer.analyze

        def slow(*args):
            time.sleep(0.5)
            return original(*args)

        analyzer.analyze = slow

        async def scenario():
            upload = asyncio.create_task(
                self.server.call_tool("upload_document", {"name": "slow.pdf", "content_base64": pdf_payload("slow")})
            )
            started = time.monotonic()
            await asyncio.sleep(0.05)
            responsive_after = time.monotonic() - started
            await upload
            return responsive_after

        self.assertLess(asyncio.run(scenario()), 0.3)


class HTTPRouteTests(ServerTestCase):
    def test_health_is_open_and_metadata_requires_token(self) -> None:
        with self.client() as client:
            self.assertEqual(client.get("/health").json(), {"status": "ok", "service": "luna-lexia"})
            self.assertEqual(client.get("/api/metadata").status_code, 401)
            self.assertEqual(client.post("/api/documents", json={}).status_code, 401)
            self.assertEqual(client.delete("/api/documents/one.pdf").status_code, 401)
            documents = client.get("/api/metadata?doc_type=payslip&financial_year=2024-25", headers=AUTH).json()["documents"]
        self.assertEqual([item["name"] for item in documents], ["one.pdf"])

    def test_public_document_route_serves_by_stable_id(self) -> None:
        with self.client() as client:
            response = client.get("/api/public/documents/doc-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.content, (self.root / "one.pdf").read_bytes())

    def test_upload_metadata_reextract_and_text_routes(self) -> None:
        with self.client() as client:
            created = client.post(
                "/api/documents", json={"name": "new.pdf", "content_base64": pdf_payload("new")}, headers=AUTH
            )
            updated = client.post(
                "/api/documents/new.pdf/metadata", json={"extracted": {"net_pay": 79000}}, headers=AUTH
            )
            reextracted = client.post("/api/documents/new.pdf/reextract", json={"doc_type": "invoice"}, headers=AUTH)
            text = client.get("/api/documents/new.pdf/text", headers=AUTH)
            missing = client.get("/api/documents/missing.pdf", headers=AUTH)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(updated.json()["primary_amount"], 79000.0)
        self.assertEqual(reextracted.json()["doc_type"], "invoice")
        self.assertIn("Net Pay", text.json()["pages"][0]["text"])
        self.assertEqual(missing.status_code, 404)

    def test_hidden_routes(self) -> None:
        with self.client() as client:
            hidden = client.post("/api/hidden/hide", json={"names": ["one.pdf"]}, headers=AUTH)
            documents = client.get("/api/hidden/documents", headers=AUTH).json()["documents"]
            updated = client.post(
                "/api/hidden/documents/one.pdf/metadata", json={"add_tags": ["Private"]}, headers=AUTH
            )
        self.assertEqual(hidden.json()["status"], "hidden")
        self.assertEqual([item["name"] for item in documents], ["one.pdf"])
        self.assertIn("private", updated.json()["tags"])

    def test_favorites_route_requires_boolean(self) -> None:
        with self.client() as client:
            response = client.post("/api/favorites", json={"names": ["one.pdf"], "favorite": "yes"}, headers=AUTH)
        self.assertEqual(response.status_code, 400)

    def test_upload_link_accepts_multipart_with_password(self) -> None:
        from test_services import LOCKED_PASSWORD, LOCKED_PDF

        url = self.call("create_upload_link", {"name": "locked"})["upload_url"]
        path = url.removeprefix("https://lexia.example.test/vault")
        with self.client() as client:
            response = client.post(
                path,
                files={"file": ("slip.pdf", LOCKED_PDF, "application/pdf")},
                data={"password": LOCKED_PASSWORD},
            )
            reused = client.post(path, content=payslip_pdf("again"))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["name"], "locked.pdf")
        self.assertEqual(reused.status_code, 404)

    def test_download_link_serves_zip(self) -> None:
        result = self.call("fetch_documents", {"names": ["two.pdf", "one.pdf"], "delivery": "zip"})
        with self.client() as client:
            response = client.get(result["download_url"].removeprefix("https://lexia.example.test/vault"))
            self.assertEqual(client.get("/api/downloads/not-a-token").status_code, 404)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn("luna-lexia-documents.zip", response.headers["content-disposition"])
        with zipfile.ZipFile(BytesIO(response.content)) as bundle:
            self.assertEqual(bundle.namelist(), ["two.pdf", "one.pdf"])
            self.assertEqual(bundle.read("one.pdf"), (self.root / "one.pdf").read_bytes())


class MediaTests(ServerTestCase):
    def test_first_page_preview_is_bounded_jpeg(self) -> None:
        renderer = PreviewRenderer(DocumentReader(), 1280)
        with PillowImage.open(BytesIO(renderer.render([self.root / "one.pdf"]))) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(max(image.size), 1280)

    def test_sheets_stay_near_square(self) -> None:
        renderer = PreviewRenderer(DocumentReader(), 1280)
        for count, columns in ((2, 2), (8, 3), (20, 5)):
            with self.subTest(count=count):
                with PillowImage.open(BytesIO(renderer.render([self.root / "one.pdf"] * count))) as image:
                    self.assertEqual(image.width, PreviewRenderer.SHEET_WIDTH // columns * columns)
                    self.assertLessEqual(image.height, image.width * 1.5)

    def test_image_documents_render_on_white(self) -> None:
        path = self.root / "logo.png"
        PillowImage.new("RGBA", (20, 30), (0, 0, 0, 0)).save(path)
        page = DocumentReader().render_page(path.read_bytes(), 0, 100)
        self.assertEqual((page.mode, page.getpixel((0, 0))), ("RGB", (255, 255, 255)))

    def test_archive_entry_names_drop_upload_prefix_and_stay_unique(self) -> None:
        names = DocumentArchive.entry_names(
            [Path(f"{'a' * 32}_form16.pdf"), Path(f"{'b' * 32}_form16.pdf"), Path("slip.pdf")]
        )
        self.assertEqual(names, ["form16.pdf", "form16 (2).pdf", "slip.pdf"])
        self.assertEqual(display_name(f"{'a' * 32}_form16.pdf"), "form16.pdf")

    def test_image_properties(self) -> None:
        properties = DocumentReader().properties(image_bytes("JPEG"))
        self.assertEqual((properties["format"], properties["page_count"]), ("JPEG", 1))


class BrandingTests(ServerTestCase):
    def test_icons_and_home_page(self) -> None:
        with self.client() as client:
            self.assertEqual(client.get("/favicon.ico").headers["content-type"], "image/x-icon")
            for size in ServerBranding.ICON_SIZES:
                with PillowImage.open(BytesIO(client.get(f"/icons/icon-{size}.png").content)) as icon:
                    self.assertEqual(icon.size, (size, size))
            self.assertNotEqual(client.get("/icons/..%2Fbranding.py").status_code, 200)
            self.assertIn("Luna Lexia", client.get("/").text)


if __name__ == "__main__":
    unittest.main()
