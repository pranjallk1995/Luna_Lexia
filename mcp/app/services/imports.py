"""Bulk imports into Lexia, from Google Drive folders or batches of uploads, as background jobs.

A job is stored with its request: a Drive folder to list, or the items to
import. Once a folder is listed, its file list replaces the request, and every
imported file is recorded with its position in that list. A job a restart cut
off therefore resumes by itself: it skips every recorded position and starts
over only the file that was in progress, without listing the folder again.

Each file is inspected (skipped if imported before unchanged, or not a PDF,
image, or Word or Excel file), downloaded, uploaded through Lexia's normal
upload (so it goes through the analysis pipeline, duplicate detection, PDF
unlocking, and Word and Excel conversion), and recorded. A
password-protected PDF is recorded as ``needs_password`` so it can be retried
with its password. Jobs run one at a time on a background worker, because each
file takes seconds of model time and a folder can hold dozens.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.schemas import document_type
from app.data.repositories import ImportRepository, Row
from app.integrations.drive import DriveClient, DriveFile, DriveOAuth
from app.integrations.remote import DocumentSourceResolver

logger = logging.getLogger(__name__)

SOURCES = ("drive_file_id", "document_url", "content_base64", "file")


class FileImporter:
    """Import one file into Lexia and record its outcome."""

    def __init__(self, visible, sources: DocumentSourceResolver, imports: ImportRepository) -> None:
        self.visible = visible
        self.sources = sources
        self.imports = imports

    def run(self, item: dict[str, Any], job_id: str, drive: DriveClient | None) -> str:
        """Import one item; return its outcome."""
        file, name = None, item.get("name") or item.get("drive_file_id") or ""
        try:
            if item.get("drive_file") or item.get("drive_file_id"):
                if drive is None:
                    raise ImportSkipped("failed", "Google Drive is not connected.")
                file = DriveFile(**item["drive_file"]) if item.get("drive_file") else drive.file(item["drive_file_id"])
                name = item.get("name") or file.upload_name
                if not file.importable:
                    raise ImportSkipped("unsupported", f"{file.mime_type} files are not imported; Lexia stores PDFs, images, and Word and Excel files.")
                if not item.get("password") and self.imports.already_imported("drive", file.file_id, file.version):
                    raise ImportSkipped("skipped")
            data, name = self.download(item, file, drive, name)
            outcome, document_name, error = *self.upload(item, name, data), None
        except ImportSkipped as stop:
            outcome, document_name, error = stop.outcome, None, stop.error
        self.imports.record_item(
            job_id,
            {
                "source": "drive" if file or item.get("drive_file_id") else self.source_of(item),
                "source_id": file.file_id if file else item.get("drive_file_id") or item.get("document_url"),
                "content_md5": file.version if file else None,
                "name": name or "(unnamed)",
                "path": file.path if file else None,
                "outcome": outcome,
                "document_name": document_name,
                "error": error,
                "position": item.get("position"),
            },
        )
        return outcome

    def download(self, item: dict[str, Any], file: DriveFile | None, drive, name: str) -> tuple[bytes, str]:
        try:
            if file is not None:
                return drive.download(file), name
            source = self.sources.resolve(item.get("content_base64"), item.get("document_url"), item.get("file"))
            return source.data, name or source.suggested_name or ""
        except Exception as error:  # noqa: BLE001 - a missing or private file is reported, not raised
            raise ImportSkipped("failed", f"Download failed: {error}"[:500]) from error

    def upload(self, item: dict[str, Any], name: str, data: bytes) -> tuple[str, str | None]:
        try:
            result = self.visible.upload(name or None, data, item.get("password"), item.get("doc_type"))
        except ValueError as error:
            message = str(error)[:500]
            raise ImportSkipped("needs_password" if "password" in message.lower() else "failed", message) from error
        except Exception as error:  # noqa: BLE001 - e.g. the model being unavailable
            raise ImportSkipped("failed", str(error)[:500]) from error
        return ("duplicate" if result.get("status") == "duplicate" else "imported"), result.get("name")

    @staticmethod
    def source_of(item: dict[str, Any]) -> str:
        return "url" if item.get("document_url") else "file" if item.get("file") else "base64"


class ImportSkipped(Exception):
    """Ends one file's import early with an outcome other than imported."""

    def __init__(self, outcome: str, error: str | None = None) -> None:
        super().__init__(error or outcome)
        self.outcome = outcome
        self.error = error


class ImportJobs:
    """Run import jobs one at a time on a background thread, resuming any a restart cut off.

    ``request`` is what a job was asked to do: ``{"folder_id", "recursive",
    "doc_type", "max_items"}`` for a Drive folder, or ``{"items": [...]}``.
    """

    def __init__(self, imports: ImportRepository, files: FileImporter, run_inline: bool = False) -> None:
        self.imports = imports
        self.files = files
        self.run_inline = run_inline
        # Set by ImportService: the connected Drive client, or None.
        self.drive_provider: Callable[[], DriveClient | None] = lambda: None
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, kind: str, source: str, request: dict[str, Any]) -> Row:
        job_id = str(self.imports.create_job(kind, source, request)["job_id"])
        self._enqueue(job_id)
        return self.imports.job(job_id)

    def resume_unfinished(self) -> list[str]:
        """Queue every job a restart interrupted, oldest first; return their IDs."""
        job_ids = [str(job_id) for job_id in self.imports.unfinished_jobs()]
        for job_id in job_ids:
            self._enqueue(job_id)
        return job_ids

    def _enqueue(self, job_id: str) -> None:
        if self.run_inline:
            self._run(job_id)
            return
        self._queue.put(job_id)
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="lexia-imports", daemon=True)
                self._worker.start()

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run(job_id)
            except Exception:  # noqa: BLE001 - one broken job never stops the worker
                logger.exception("Import job %s failed", job_id)
                self.imports.update_job(job_id, status="failed", error="Unexpected error; see the logs.", finished=True)
            finally:
                self._queue.task_done()

    def _run(self, job_id: str) -> None:
        """Start a job, or continue it past the files it already recorded."""
        request, drive = self.imports.job_request(job_id) or {}, self.drive_provider()
        if "items" not in request:
            self.imports.update_job(job_id, status="listing", started=True)
            try:
                listed = {"items": self.plan(request, drive)}
            except Exception as error:  # noqa: BLE001 - recorded on the job
                # Kept so a retry can list the folder again.
                self.imports.update_job(
                    job_id, status="failed", error=str(error)[:1000], request={"rerun": request}, finished=True
                )
                return
            request = listed
            # The listed files replace the request, so a resume never lists the folder again.
            self.imports.update_job(job_id, request=request)
        items = request["items"]
        self.imports.update_job(job_id, status="running", total=len(items), started=True)
        for position, item in enumerate(items):
            if self.imports.item_recorded(job_id, position):
                continue
            self.imports.update_job(job_id, current_item=self.item_name(item))
            try:
                self.files.run({**item, "position": position}, job_id, drive)
            except Exception as error:  # noqa: BLE001 - one bad file never stops the job
                logger.exception("Import item failed")
                self.imports.record_item(job_id, {"source": "unknown", "name": str(item.get("name") or "?"),
                                                  "outcome": "failed", "error": str(error)[:500], "position": position})
        # The request may hold Base64 uploads, so only the failed files' items are
        # kept, for a retry.
        failed = [
            items[row["position"]]
            for row in self.imports.items(job_id)
            if row["outcome"] == "failed" and row.get("position") is not None and row["position"] < len(items)
        ]
        self.imports.update_job(
            job_id, status="done", current_item=None, request={"retry": failed} if failed else None, finished=True
        )

    @staticmethod
    def item_name(item: dict[str, Any]) -> str:
        """What to call an item while it is being imported."""
        drive_file = item.get("drive_file") or {}
        file = item.get("file") or {}
        return str(
            item.get("name")
            or f"{drive_file.get('path') or ''}{drive_file.get('name') or ''}"
            or file.get("file_name")
            or file.get("name")
            or item.get("document_url")
            or item.get("drive_file_id")
            or "upload"
        )[:300]

    @staticmethod
    def plan(request: dict[str, Any], drive: DriveClient | None) -> list[dict[str, Any]]:
        """List a Drive folder's files as import items."""
        if drive is None:
            raise ValueError("Google Drive is not connected.")
        files = drive.walk(request["folder_id"], request.get("recursive", True))
        limit = request.get("max_items")
        if limit and len(files) > limit:
            raise ValueError(f"One import may hold at most {limit} files; this one has {len(files)}.")
        return [{"drive_file": asdict(file), "doc_type": request.get("doc_type")} for file in files]


class ImportService:
    """Connect Google Drive and queue bulk imports; the transport adapters call this."""

    def __init__(
        self,
        imports: ImportRepository,
        oauth: DriveOAuth,
        jobs: ImportJobs,
        encryption_key: str,
        max_items: int,
        drive_factory: Callable[[str], DriveClient] | None = None,
    ) -> None:
        self.imports = imports
        self.oauth = oauth
        self.jobs = jobs
        self.cipher = Fernet(encryption_key.encode()) if encryption_key else None
        self.max_items = max_items
        self.drive_factory = drive_factory or (
            lambda token: DriveClient.from_refresh_token(token, oauth.client_id, oauth.client_secret)
        )
        jobs.drive_provider = self.connected_drive

    # Connection

    def drive_status(self) -> dict[str, Any]:
        connection = self.imports.drive_connection()
        return {
            "configured": self.oauth.configured and self.cipher is not None,
            "connected": connection is not None,
            "email": connection["email"] if connection else None,
            "connected_at": connection["connected_at"] if connection else None,
        }

    def authorization_url(self) -> str:
        if self.cipher is None:
            raise ValueError("Set LUNALEXIA_TOKEN_ENCRYPTION_KEY before connecting Google Drive.")
        return self.oauth.authorization_url()

    def connect(self, callback_url: str, state: str) -> str:
        email, refresh_token = self.oauth.exchange(callback_url, state)
        self.imports.save_drive_connection(email, self.cipher.encrypt(refresh_token.encode()).decode())
        return email

    def disconnect(self) -> dict[str, Any]:
        self.imports.delete_drive_connection()
        return {"status": "disconnected"}

    def drive(self) -> DriveClient:
        connection = self.imports.drive_connection()
        if connection is None or self.cipher is None:
            raise ValueError("Google Drive is not connected; connect it from Lexia's upload dialog first.")
        try:
            token = self.cipher.decrypt(connection["encrypted_refresh_token"].encode()).decode()
        except InvalidToken as error:
            raise ValueError("The stored Drive token cannot be read; connect Google Drive again.") from error
        return self.drive_factory(token)

    def connected_drive(self) -> DriveClient | None:
        """The Drive client for a job about to run, or None when Drive is disconnected."""
        try:
            return self.drive()
        except ValueError:
            return None

    def drive_folders(self, parent_id: str | None = None) -> list[dict]:
        return self.drive().folders(parent_id)

    # Jobs

    def _check_count(self, count: int) -> None:
        if count > self.max_items:
            raise ValueError(f"One import may hold at most {self.max_items} files; this one has {count}.")

    def import_drive_folder(self, folder: str, recursive: bool = True, doc_type: str | None = None) -> Row:
        """Queue every file under a Drive folder (link, ID, or exact name)."""
        if doc_type:
            document_type(doc_type)
        target = self.drive().resolve_folder(folder)
        request = {"folder_id": target["folder_id"], "recursive": recursive, "doc_type": doc_type,
                   "max_items": self.max_items}
        return self.jobs.submit("drive_folder", f"{target['name']} ({target['folder_id']})", request)

    def upload_batch(self, items: list[dict[str, Any]]) -> Row:
        """Queue many uploads from attachments, URLs, Base64, or Drive file IDs."""
        if not items:
            raise ValueError("Provide at least one document to upload.")
        self._check_count(len(items))
        for index, item in enumerate(items, start=1):
            given = [key for key in SOURCES if item.get(key)]
            if len(given) != 1:
                raise ValueError(f"Item {index} must have exactly one of {', '.join(SOURCES)}.")
            if item.get("doc_type"):
                document_type(item["doc_type"])
        if any(item.get("drive_file_id") for item in items):
            self.drive()
        return self.jobs.submit("batch_upload", f"{len(items)} upload(s)", {"items": items})

    def job(self, job_id: str, include_items: bool = True) -> Row:
        job = self.imports.job(job_id)
        if job is None:
            raise FileNotFoundError(f"No import job has ID {job_id}.")
        job["retryable"] = self._retry_request(job) is not None
        if include_items:
            job["items"] = self.imports.items(job_id)
        return job

    def jobs_list(self, limit: int = 20) -> list[Row]:
        jobs = self.imports.jobs(max(1, min(limit, 100)))
        for job in jobs:
            job["retryable"] = self._retry_request(job) is not None
        return jobs

    def _retry_request(self, job: Row) -> dict[str, Any] | None:
        """What retrying a finished job queues: its failed files, or a folder whose listing failed.

        Jobs from before failed items were kept fall back to their recorded
        Drive files and URLs. A job that was already retried has nothing left.
        """
        if job["status"] not in ("done", "failed"):
            return None
        request = self.imports.job_request(str(job["job_id"])) or {}
        if "retried" in request:
            return None
        if "rerun" in request:
            return request["rerun"]
        if job["status"] == "failed" and "items" in request:
            return request  # an unexpected error stopped it; files already imported come back as duplicates
        items = request.get("retry") or [
            {"drive_file_id": row["source_id"]} if row["source"] == "drive" else {"document_url": row["source_id"]}
            for row in self.imports.items(str(job["job_id"]))
            if row["outcome"] == "failed" and row["source"] in ("drive", "url") and row.get("source_id")
        ]
        return {"items": items} if items else None

    def retry(self, job_id: str) -> Row:
        """Queue a finished job's failed files (or its whole folder, if listing it failed) as a new job."""
        job = self.job(job_id, include_items=False)
        request = self._retry_request(job)
        if request is None:
            if job["status"] not in ("done", "failed"):
                raise ValueError("This import is still running; retry it once it has finished.")
            raise ValueError("This import has no failed files to retry, or was already retried.")
        if any(item.get("drive_file_id") or item.get("drive_file") for item in request.get("items", [])):
            self.drive()
        source = f"Retry of {job['source'].removeprefix('Retry of ')}"
        retry = self.jobs.submit(job["kind"], source, request)
        self.imports.update_job(job_id, request={"retried": str(retry["job_id"])})
        return retry
