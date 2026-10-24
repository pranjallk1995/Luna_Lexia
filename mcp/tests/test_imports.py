"""Tests for Google Drive imports, bulk uploads, and bulk downloads."""

import base64
import tempfile
import unittest
import uuid
from pathlib import Path

from cryptography.fernet import Fernet
from langgraph.checkpoint.memory import InMemorySaver

from fakes import FakeAnalyzer, FakeDocumentRepository, FakeParser, FakePublicURLRepository, image_bytes, payslip_pdf
from test_services import LOCKED_PASSWORD, LOCKED_PDF

from app.core.config import AppConfig
from app.integrations.drive import DriveFile, DriveOAuth
from app.integrations.remote import DocumentSourceResolver
from app.services.application import LexiaService
from app.services.imports import ImportGraph, ImportJobs, ImportService


class FakeImportRepository:
    """An in-memory ImportRepository."""

    IMPORTED_OUTCOMES = ("imported", "duplicate")

    def __init__(self) -> None:
        self.connection = None
        self.jobs_by_id: dict[str, dict] = {}
        self.item_rows: list[dict] = []
        self.requests: dict[str, dict | None] = {}

    def drive_connection(self):
        return self.connection

    def save_drive_connection(self, email, encrypted_refresh_token):
        self.connection = {"email": email, "encrypted_refresh_token": encrypted_refresh_token, "connected_at": "now"}

    def delete_drive_connection(self):
        self.connection = None

    def create_job(self, kind, source, request):
        job_id = str(uuid.uuid4())
        self.jobs_by_id[job_id] = {"job_id": job_id, "kind": kind, "source": source, "status": "queued", "total": 0, "error": None}
        self.requests[job_id] = request
        return dict(self.jobs_by_id[job_id])

    def update_job(self, job_id, **fields):
        if "request" in fields:
            self.requests[job_id] = fields.pop("request")
        self.jobs_by_id[job_id].update({key: value for key, value in fields.items() if key not in ("started", "finished")})

    def job_request(self, job_id):
        return self.requests.get(job_id)

    def unfinished_jobs(self):
        return [job_id for job_id, job in self.jobs_by_id.items() if job["status"] in ("queued", "listing", "running")]

    def item_recorded(self, job_id, position):
        return any(row["job_id"] == job_id and row.get("position") == position for row in self.item_rows)

    def job(self, job_id):
        job = self.jobs_by_id.get(job_id)
        if job is None:
            return None
        items = self.items(job_id)
        outcomes: dict[str, int] = {}
        for item in items:
            outcomes[item["outcome"]] = outcomes.get(item["outcome"], 0) + 1
        return {**job, "processed": len(items), "outcomes": outcomes}

    def jobs(self, limit):
        return [self.job(job_id) for job_id in list(self.jobs_by_id)[::-1][:limit]]

    def items(self, job_id):
        return [row for row in self.item_rows if row["job_id"] == job_id]

    def record_item(self, job_id, item):
        self.item_rows.append({"job_id": job_id, **item})

    def already_imported(self, source, source_id, content_md5):
        return next(
            (
                row for row in reversed(self.item_rows)
                if row["source"] == source and row["source_id"] == source_id
                and row["outcome"] in self.IMPORTED_OUTCOMES
                and (content_md5 is None or row["content_md5"] == content_md5)
            ),
            None,
        )


class FakeDrive:
    """A Drive folder tree: Professional/ with a payslip, a scan, a Google Doc, a locked PDF, and a ZIP."""

    def __init__(self) -> None:
        self.files = {
            "f-slip": (DriveFile("f-slip", "payslip_march.pdf", "application/pdf", "md5-a", path="Tranzeal/"), payslip_pdf("march")),
            "f-scan": (DriveFile("f-scan", "pan.png", "image/png", "md5-b", path=""), image_bytes()),
            "f-doc": (DriveFile("f-doc", "Resume 2026", "application/vnd.google-apps.document", None, modified="2026-09-18T07:00:00Z"), payslip_pdf("resume")),
            "f-lock": (DriveFile("f-lock", "locked_slip.pdf", "application/pdf", "md5-c", path="Tranzeal/"), LOCKED_PDF),
            "f-zip": (DriveFile("f-zip", "code.zip", "application/zip", "md5-d"), b"PK"),
        }
        self.downloads = 0
        self.walks = 0

    def resolve_folder(self, spec):
        if spec == "Missing":
            raise FileNotFoundError("No Drive folder is named 'Missing'.")
        return {"folder_id": "folder-1", "name": spec}

    def walk(self, folder_id, recursive=True, path=""):
        self.walks += 1
        return [file for file, _ in self.files.values() if recursive or not file.path]

    def folders(self, parent_id=None):
        return [{"folder_id": "folder-1", "name": "Professional"}]

    def file(self, file_id):
        return self.files[file_id][0]

    def download(self, file):
        self.downloads += 1
        return self.files[file.file_id][1]


class FakeOAuth(DriveOAuth):
    def __init__(self) -> None:
        super().__init__("client", "secret", "http://localhost:18004/api/drive/callback", "x" * 32)

    def exchange(self, callback_url, state):
        return "me@example.com", "refresh-token"


class ImportTestCase(unittest.TestCase):
    def setUp(self):
        self.temp, self.downloads_temp = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = FakeDocumentRepository()
        config = AppConfig(
            document_dir=self.root,
            database_url="postgresql://test",
            ollama_url="http://ollama:11434",
            document_model="test-model",
            public_base_url="https://lexia.example.test",
            download_dir=Path(self.downloads_temp.name),
        )
        self.service = LexiaService.create(
            config, self.repository, FakePublicURLRepository(self.repository), FakeParser(), FakeAnalyzer()
        )
        self.imports_repo = FakeImportRepository()
        self.drive = FakeDrive()
        self.tokens = []
        sources = DocumentSourceResolver(self.service.visible.decoder, config.max_document_bytes, 3)
        graph = ImportGraph(self.service.visible, sources, self.imports_repo)
        self.imports = ImportService(
            self.imports_repo,
            FakeOAuth(),
            ImportJobs(self.imports_repo, graph, InMemorySaver(), run_inline=True),
            Fernet.generate_key().decode(),
            500,
            drive_factory=lambda token: self.tokens.append(token) or self.drive,
        )

    def tearDown(self):
        self.temp.cleanup()
        self.downloads_temp.cleanup()

    def connect(self):
        self.imports.connect("http://localhost:18004/api/drive/callback?code=x&state=y", "y")


class DriveConnectionTests(ImportTestCase):
    def test_connection_stores_an_encrypted_token(self):
        self.assertFalse(self.imports.drive_status()["connected"])
        self.connect()
        status = self.imports.drive_status()
        self.assertEqual((status["connected"], status["email"]), (True, "me@example.com"))
        self.assertNotIn("refresh-token", self.imports_repo.connection["encrypted_refresh_token"])
        self.imports.drive_folders()
        self.assertEqual(self.tokens, ["refresh-token"])

    def test_imports_need_a_connection(self):
        with self.assertRaisesRegex(ValueError, "not connected"):
            self.imports.import_drive_folder("Professional")

    def test_authorization_url_is_read_only_and_signed(self):
        url = self.imports.authorization_url()
        self.assertIn("drive.readonly", url)
        self.assertIn("state=", url)
        self.assertIn("access_type=offline", url)


class DriveFolderImportTests(ImportTestCase):
    def setUp(self):
        super().setUp()
        self.connect()

    def outcomes(self, job):
        return {item["name"]: item["outcome"] for item in self.imports.job(str(job["job_id"]))["items"]}

    def test_folder_import_reports_every_file(self):
        job = self.imports.import_drive_folder("Professional")
        self.assertEqual(
            self.outcomes(job),
            {
                "payslip_march.pdf": "imported",
                "pan.png": "imported",
                "Resume 2026.pdf": "imported",
                "locked_slip.pdf": "needs_password",
                "code.zip": "unsupported",
            },
        )
        report = self.imports.job(str(job["job_id"]))
        self.assertEqual((report["status"], report["total"], report["processed"]), ("done", 5, 5))
        self.assertTrue((self.root / "Resume 2026.pdf").is_file())
        paths = {item["name"]: item["path"] for item in report["items"]}
        self.assertEqual(paths["payslip_march.pdf"], "Tranzeal/")

    def test_rerunning_a_folder_skips_unchanged_files(self):
        self.imports.import_drive_folder("Professional")
        downloads = self.drive.downloads
        outcomes = self.outcomes(self.imports.import_drive_folder("Professional"))
        self.assertEqual(outcomes["payslip_march.pdf"], "skipped")
        self.assertEqual(outcomes["Resume 2026.pdf"], "skipped")
        self.assertEqual(outcomes["locked_slip.pdf"], "needs_password")
        self.assertEqual(self.drive.downloads - downloads, 1)  # only the locked PDF is fetched again

    def test_a_changed_file_is_imported_again(self):
        self.imports.import_drive_folder("Professional")
        file, data = self.drive.files["f-slip"]
        self.drive.files["f-slip"] = (DriveFile(file.file_id, file.name, file.mime_type, "md5-new", path=file.path), payslip_pdf("revised"))
        self.assertEqual(self.outcomes(self.imports.import_drive_folder("Professional"))["payslip_march.pdf"], "imported")

    def test_locked_files_can_be_retried_with_their_password(self):
        self.imports.import_drive_folder("Professional")
        job = self.imports.import_drive_files([{"file_id": "f-lock", "password": LOCKED_PASSWORD}])
        self.assertEqual(self.outcomes(job), {"locked_slip.pdf": "imported"})

    def test_folder_problems_are_reported(self):
        with self.assertRaisesRegex(FileNotFoundError, "Missing"):
            self.imports.import_drive_folder("Missing")
        with self.assertRaisesRegex(ValueError, "Unknown doc_type"):
            self.imports.import_drive_folder("Professional", doc_type="receipt")

    def test_folder_size_is_capped(self):
        self.imports.max_items = 2
        job = self.imports.import_drive_folder("Professional")
        report = self.imports.job(str(job["job_id"]))
        self.assertEqual(report["status"], "failed")
        self.assertIn("at most 2 files", report["error"])


class BulkUploadTests(ImportTestCase):
    def test_batch_upload_imports_each_item_and_reports_duplicates(self):
        payload = base64.b64encode(payslip_pdf("batch")).decode()
        job = self.imports.upload_batch(
            [
                {"name": "batch_one.pdf", "content_base64": payload},
                {"name": "batch_again.pdf", "content_base64": payload},
                {"name": "scan.png", "content_base64": base64.b64encode(image_bytes()).decode(), "doc_type": "identity_document"},
            ]
        )
        report = self.imports.job(str(job["job_id"]))
        self.assertEqual([item["outcome"] for item in report["items"]], ["imported", "duplicate", "imported"])
        self.assertEqual(report["items"][1]["document_name"], "batch_one.pdf")
        self.assertEqual(self.repository.rows["scan.png"]["doc_type"], "identity_document")

    def test_each_item_needs_exactly_one_source(self):
        with self.assertRaisesRegex(ValueError, "Item 1 must have exactly one"):
            self.imports.upload_batch([{"name": "x.pdf"}])
        with self.assertRaisesRegex(ValueError, "Item 2 must have exactly one"):
            self.imports.upload_batch([{"content_base64": "eA=="}, {"content_base64": "eA==", "document_url": "https://x"}])
        with self.assertRaisesRegex(ValueError, "at least one"):
            self.imports.upload_batch([])

    def test_unreadable_items_fail_alone(self):
        job = self.imports.upload_batch([{"name": "bad.pdf", "content_base64": "not base64!"}, {"name": "ok.pdf", "content_base64": base64.b64encode(payslip_pdf("ok")).decode()}])
        self.assertEqual([item["outcome"] for item in self.imports.job(str(job["job_id"]))["items"]], ["failed", "imported"])


class BulkDownloadTests(ImportTestCase):
    def test_zip_by_type_tags_or_names(self):
        self.service.visible.ingest("march.pdf", base64.b64encode(payslip_pdf("m")).decode())
        self.service.visible.ingest("form.pdf", base64.b64encode(payslip_pdf("f")).decode(), doc_type="form_16")
        downloads = self.service.downloads
        self.assertEqual(downloads.create_zip_for(doc_types=["payslip"])["names"], ["march.pdf"])
        self.assertEqual(downloads.create_zip_for(tags=["form 16"])["names"], ["form.pdf"])
        self.assertEqual(downloads.create_zip_for(names=["form.pdf", "march.pdf"])["document_count"], 2)
        with self.assertRaisesRegex(ValueError, "Choose documents"):
            downloads.create_zip_for()
        with self.assertRaises(FileNotFoundError):
            downloads.create_zip_for(financial_year="2001-02")


class TransportTests(ImportTestCase):
    def setUp(self):
        super().setUp()
        import asyncio

        from mcp.server.fastmcp import FastMCP
        from starlette.testclient import TestClient

        from app.transport.branding import ServerBranding
        from app.transport.http_routes import LexiaHTTPRoutes
        from app.transport.mcp_tools import LexiaMCPTools

        self.service.imports = self.imports
        server = FastMCP("Lexia test", host="0.0.0.0")
        LexiaMCPTools(server, self.service, DocumentSourceResolver(self.service.visible.decoder, 10**6, 3))
        LexiaHTTPRoutes(server, self.service, ServerBranding(), "x" * 32)
        self.tools = {tool.name for tool in asyncio.run(server.list_tools())}
        self.client = TestClient(server.streamable_http_app())
        self.auth = {"Authorization": f"Bearer {'x' * 32}"}

    def test_import_tools_are_registered(self):
        expected = {
            "google_drive_status", "list_drive_folders", "import_drive_folder", "import_drive_files",
            "upload_documents", "get_import_job", "list_import_jobs", "download_documents",
        }
        self.assertTrue(expected <= self.tools, expected - self.tools)

    def test_routes_need_the_token_except_the_google_callback(self):
        with self.client as client:
            self.assertEqual(client.get("/api/drive/status").status_code, 401)
            self.assertEqual(client.post("/api/imports/batch", json={}).status_code, 401)
            self.assertFalse(client.get("/api/drive/status", headers=self.auth).json()["connected"])
            denied = client.get("/api/drive/callback?error=access_denied")
            connected = client.get("/api/drive/callback?code=abc&state=signed")
            job = client.post(
                "/api/imports/batch",
                json={"items": [{"name": "r.pdf", "content_base64": base64.b64encode(payslip_pdf("r")).decode()}]},
                headers=self.auth,
            ).json()
            report = client.get(f"/api/imports/{job['job_id']}", headers=self.auth).json()
        self.assertIn("was not connected", denied.text)
        self.assertIn("Signed in as me@example.com", connected.text)
        self.assertEqual([item["outcome"] for item in report["items"]], ["imported"])


class DriveConfigTests(unittest.TestCase):
    def test_google_client_comes_from_the_ui_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secrets = Path(directory) / "secrets.toml"
            secrets.write_text('[auth]\nclient_id = "id.apps"\nclient_secret = "shh"\n')
            self.assertEqual(AppConfig.google_client(secrets), ("id.apps", "shh"))
            self.assertEqual(AppConfig.google_client(Path(directory) / "missing.toml"), ("", ""))

    def test_encryption_key_is_a_stable_fernet_key(self) -> None:
        key = AppConfig.derive_encryption_key("t" * 32)
        self.assertEqual(key, AppConfig.derive_encryption_key("t" * 32))
        self.assertNotEqual(key, AppConfig.derive_encryption_key("u" * 32))
        self.assertEqual(Fernet(key).decrypt(Fernet(key).encrypt(b"refresh")), b"refresh")


class Interruption(BaseException):
    """Stands in for the process dying: not an Exception, so nothing in the graphs catches it."""


class ResumeTests(ImportTestCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.uploaded = []
        upload = self.service.visible.upload

        def tracked_upload(name, *args):
            self.uploaded.append(name)
            if self.crash_at == len(self.uploaded):
                raise Interruption
            return upload(name, *args)

        self.crash_at = 2
        self.service.visible.upload = tracked_upload

    def job_id(self):
        return next(iter(self.imports_repo.jobs_by_id))

    def test_an_interrupted_folder_import_resumes_at_the_file_it_was_on(self):
        with self.assertRaises(Interruption):
            self.imports.import_drive_folder("Professional")
        stopped = self.imports.job(self.job_id())
        self.assertEqual((stopped["status"], stopped["processed"]), ("running", 1))

        self.crash_at = None
        self.assertEqual(self.imports.jobs.resume_unfinished(), [self.job_id()])
        report = self.imports.job(self.job_id())
        self.assertEqual((report["status"], report["total"], report["processed"]), ("done", 5, 5))
        self.assertEqual(self.drive.walks, 1)  # the file list came from the checkpoint
        self.assertEqual(self.uploaded.count("payslip_march.pdf"), 1)  # finished files are not redone
        self.assertEqual(self.uploaded.count("pan.png"), 2)  # the file in progress starts over
        self.assertEqual(report["outcomes"], {"imported": 3, "needs_password": 1, "unsupported": 1})
        self.assertIsNone(self.imports_repo.requests[self.job_id()])
        self.assertFalse(self.imports.jobs.graph.get_state({"configurable": {"thread_id": self.job_id()}}).values)

    def test_a_file_recorded_just_before_the_interruption_is_not_imported_again(self):
        with self.assertRaises(Interruption):
            self.imports.import_drive_folder("Professional")
        # As if the process died after pan.png was recorded but before its step was checkpointed.
        self.imports_repo.record_item(self.job_id(), {"source": "drive", "source_id": "f-scan", "name": "pan.png",
                                                      "outcome": "imported", "position": 1})
        self.crash_at = None
        self.imports.jobs.resume_unfinished()
        self.assertEqual(self.uploaded.count("pan.png"), 1)
        self.assertEqual(self.imports.job(self.job_id())["processed"], 5)

    def test_a_queued_job_that_never_started_runs_from_its_stored_request(self):
        payload = base64.b64encode(payslip_pdf("queued")).decode()
        job = self.imports_repo.create_job("batch_upload", "1 upload(s)", {"items": [{"name": "q.pdf", "content_base64": payload}]})
        self.crash_at = None
        self.imports.jobs.resume_unfinished()
        report = self.imports.job(str(job["job_id"]))
        self.assertEqual((report["status"], report["outcomes"]), ("done", {"imported": 1}))


if __name__ == "__main__":
    unittest.main()
