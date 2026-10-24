"""Bulk imports into Lexia, from Google Drive folders or batches of uploads, as background jobs.

Two LangGraph graphs do the work. A job runs as a checkpointed graph whose
thread is the job ID:

    plan ─┬─▶ import_next ⟲ (one file per step) ─▶ finish
          └─▶ finish                                (nothing to import, or listing failed)

After every step the checkpointer saves the job's state (its file list and a
cursor), so a job cut off by a restart resumes at the file it was on instead of
listing the folder and starting again. Each file runs through its own graph:

    inspect ─┬─▶ record                        (already imported unchanged, or not a PDF or image)
             └─▶ download ─┬─▶ upload ─▶ record
                           └─▶ record           (download failed)

``upload`` hands the bytes to Lexia's normal upload, so each file goes through
the document analysis graph, duplicate detection, and PDF unlocking. A
password-protected PDF is recorded as ``needs_password`` so it can be retried
with its password. Jobs run one at a time on a background worker, because each
file takes seconds of model time and a folder can hold dozens.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, TypedDict

from cryptography.fernet import Fernet, InvalidToken
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.core.schemas import document_type
from app.data.repositories import ImportRepository, Row
from app.integrations.drive import DriveClient, DriveFile, DriveOAuth
from app.integrations.remote import DocumentSourceResolver

logger = logging.getLogger(__name__)

SOURCES = ("drive_file_id", "document_url", "content_base64", "file")


@dataclass(frozen=True)
class ImportContext:
    """What every file of one job shares."""

    job_id: str
    drive: DriveClient | None


class ImportState(TypedDict, total=False):
    item: dict[str, Any]
    drive_file: DriveFile | None
    name: str
    data: bytes
    outcome: str
    document_name: str | None
    error: str | None


class ImportGraph:
    """Import one file: inspect it, download it, upload it into Lexia, and record the outcome."""

    def __init__(self, visible, sources: DocumentSourceResolver, imports: ImportRepository) -> None:
        self.visible = visible
        self.sources = sources
        self.imports = imports
        graph = StateGraph(ImportState, context_schema=ImportContext)
        graph.add_node("inspect", self.inspect)
        graph.add_node("download", self.download)
        graph.add_node("upload", self.upload)
        graph.add_node("record", self.record)
        graph.add_edge(START, "inspect")
        graph.add_conditional_edges("inspect", lambda s: "record" if s.get("outcome") else "download", ["record", "download"])
        graph.add_conditional_edges("download", lambda s: "record" if s.get("outcome") else "upload", ["record", "upload"])
        graph.add_edge("upload", "record")
        graph.add_edge("record", END)
        # Never checkpointed: a file's state holds its bytes, and the job graph resumes per file.
        self.graph = graph.compile(checkpointer=False, name="lexia-import")

    def run(self, item: dict[str, Any], job_id: str, drive: DriveClient | None) -> str:
        """Import one item; return its outcome."""
        state = self.graph.invoke({"item": item}, context=ImportContext(job_id, drive), durability="exit")
        return state["outcome"]

    # Nodes

    def inspect(self, state: ImportState, runtime: Runtime[ImportContext]) -> ImportState:
        item = state["item"]
        if not item.get("drive_file_id") and not item.get("drive_file"):
            return {"name": item.get("name") or "", "drive_file": None}
        drive = runtime.context.drive
        if drive is None:
            return {"name": item.get("name") or item.get("drive_file_id", ""), "outcome": "failed",
                    "error": "Google Drive is not connected."}
        try:
            file = DriveFile(**item["drive_file"]) if item.get("drive_file") else drive.file(item["drive_file_id"])
        except Exception as error:  # noqa: BLE001 - a missing or private file is reported, not raised
            return {"name": item.get("drive_file_id", ""), "outcome": "failed", "error": str(error)[:500]}
        name = item.get("name") or file.upload_name
        if not file.importable:
            return {"drive_file": file, "name": name, "outcome": "unsupported",
                    "error": f"{file.mime_type} files are not imported; Lexia stores PDFs and images."}
        if not item.get("password") and self.imports.already_imported("drive", file.file_id, file.version):
            return {"drive_file": file, "name": name, "outcome": "skipped"}
        return {"drive_file": file, "name": name}

    def download(self, state: ImportState, runtime: Runtime[ImportContext]) -> ImportState:
        item, file = state["item"], state.get("drive_file")
        try:
            if file is not None:
                return {"data": runtime.context.drive.download(file)}
            source = self.sources.resolve(item.get("content_base64"), item.get("document_url"), item.get("file"))
            return {"data": source.data, "name": state["name"] or source.suggested_name or ""}
        except Exception as error:  # noqa: BLE001
            return {"outcome": "failed", "error": f"Download failed: {error}"[:500]}

    def upload(self, state: ImportState) -> ImportState:
        item = state["item"]
        try:
            result = self.visible.upload(state["name"] or None, state["data"], item.get("password"), item.get("doc_type"))
        except ValueError as error:
            message = str(error)
            if "password" in message.lower():
                return {"outcome": "needs_password", "error": message[:500]}
            return {"outcome": "failed", "error": message[:500]}
        except Exception as error:  # noqa: BLE001 - e.g. the model being unavailable
            return {"outcome": "failed", "error": str(error)[:500]}
        outcome = "duplicate" if result.get("status") == "duplicate" else "imported"
        return {"outcome": outcome, "document_name": result.get("name")}

    def record(self, state: ImportState, runtime: Runtime[ImportContext]) -> ImportState:
        file, item = state.get("drive_file"), state["item"]
        self.imports.record_item(
            runtime.context.job_id,
            {
                "source": "drive" if file or item.get("drive_file_id") else self.source_of(item),
                "source_id": file.file_id if file else item.get("drive_file_id") or item.get("document_url"),
                "content_md5": file.version if file else None,
                "name": state.get("name") or "(unnamed)",
                "path": file.path if file else None,
                "outcome": state["outcome"],
                "document_name": state.get("document_name"),
                "error": state.get("error"),
                "position": item.get("position"),
            },
        )
        return {}

    # Routes and helpers

    @staticmethod
    def source_of(item: dict[str, Any]) -> str:
        return "url" if item.get("document_url") else "file" if item.get("file") else "base64"


class JobState(TypedDict, total=False):
    request: dict[str, Any]
    items: list[dict[str, Any]]
    cursor: int
    error: str | None


class ImportJobs:
    """Run import jobs one at a time on a background thread, resuming any a restart cut off.

    ``request`` is what a job was asked to do: ``{"folder_id", "recursive",
    "doc_type", "max_items"}`` for a Drive folder, or ``{"items": [...]}``. It is
    stored with the job, so a job queued but not yet started also survives a restart.
    """

    # One graph step per file, so the limit must exceed the largest job.
    RECURSION_LIMIT = 10_000

    def __init__(
        self,
        imports: ImportRepository,
        graph: ImportGraph,
        checkpointer: BaseCheckpointSaver,
        run_inline: bool = False,
    ) -> None:
        self.imports = imports
        self.files = graph
        self.checkpointer = checkpointer
        self.run_inline = run_inline
        # Set by ImportService: the connected Drive client, or None.
        self.drive_provider: Callable[[], DriveClient | None] = lambda: None
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        job = StateGraph(JobState, context_schema=ImportContext)
        job.add_node("plan", self.plan)
        job.add_node("import_next", self.import_next)
        job.add_node("finish", self.finish)
        job.add_edge(START, "plan")
        job.add_conditional_edges("plan", self.route, ["import_next", "finish"])
        job.add_conditional_edges("import_next", self.route, ["import_next", "finish"])
        job.add_edge("finish", END)
        self.graph = job.compile(checkpointer=checkpointer, name="lexia-import-job")

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
        """Start a job, or continue it from its last checkpoint."""
        config = {"configurable": {"thread_id": job_id}, "recursion_limit": self.RECURSION_LIMIT}
        if self.graph.get_state(config).next:
            graph_input = None
            logger.info("Resuming import job %s from its checkpoint", job_id)
        else:
            self.imports.update_job(job_id, status="listing", started=True)
            graph_input = {"request": self.imports.job_request(job_id) or {}, "cursor": 0}
        self.graph.invoke(graph_input, config, context=ImportContext(job_id, self.drive_provider()), durability="sync")
        self.checkpointer.delete_thread(job_id)

    # Nodes

    def plan(self, state: JobState, runtime: Runtime[ImportContext]) -> JobState:
        """List the job's files once; the checkpoint keeps the list for a resume."""
        request = state["request"]
        try:
            if "folder_id" in request:
                drive = runtime.context.drive
                if drive is None:
                    raise ValueError("Google Drive is not connected.")
                files = drive.walk(request["folder_id"], request.get("recursive", True))
                items = [{"drive_file": asdict(file), "doc_type": request.get("doc_type")} for file in files]
            else:
                items = list(request.get("items") or [])
            limit = request.get("max_items")
            if limit and len(items) > limit:
                raise ValueError(f"One import may hold at most {limit} files; this one has {len(items)}.")
        except Exception as error:  # noqa: BLE001 - recorded on the job
            return {"items": [], "error": str(error)[:1000]}
        self.imports.update_job(runtime.context.job_id, status="running", total=len(items))
        return {"items": items, "cursor": 0}

    def import_next(self, state: JobState, runtime: Runtime[ImportContext]) -> JobState:
        """Import the file at the cursor, unless it was recorded just before an interruption."""
        cursor, job_id = state["cursor"], runtime.context.job_id
        item = {**state["items"][cursor], "position": cursor}
        if not self.imports.item_recorded(job_id, cursor):
            try:
                self.files.run(item, job_id, runtime.context.drive)
            except Exception as error:  # noqa: BLE001 - one bad file never stops the job
                logger.exception("Import item failed")
                self.imports.record_item(job_id, {"source": "unknown", "name": str(item.get("name") or "?"),
                                                  "outcome": "failed", "error": str(error)[:500], "position": cursor})
        return {"cursor": cursor + 1}

    def finish(self, state: JobState, runtime: Runtime[ImportContext]) -> JobState:
        error = state.get("error")
        # The request may hold Base64 uploads; it is not needed once the job is over.
        self.imports.update_job(
            runtime.context.job_id, status="failed" if error else "done", error=error, request=None, finished=True
        )
        return {}

    @staticmethod
    def route(state: JobState) -> str:
        return "import_next" if state.get("cursor", 0) < len(state.get("items") or []) else "finish"


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
        if include_items:
            job["items"] = self.imports.items(job_id)
        return job

    def jobs_list(self, limit: int = 20) -> list[Row]:
        return self.imports.jobs(max(1, min(limit, 100)))
