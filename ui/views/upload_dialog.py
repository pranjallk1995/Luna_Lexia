"""Upload dialog: send documents for parsing and extraction, and show the results."""

import base64
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
import streamlit as st

from services.api import LunaLexiaAPI
from views.drive_panel import DriveImportPanel
from views.shared import DocumentLabel, LexiaTab


class UploadDialog(LexiaTab):
    """Upload new files, retry failures, and keep the returned details."""

    PROGRESS_TICK_SECONDS = 0.5

    def open(self) -> None:
        @st.dialog("Upload documents", width="large")
        def upload_dialog() -> None:
            device_tab, drive_tab = st.tabs([":material/devices: This device", ":material/add_to_drive: Google Drive"])
            with device_tab:
                self.render()
            with drive_tab:
                DriveImportPanel(self.context).render()

        upload_dialog()

    @staticmethod
    def stored_name(display_name: str) -> str:
        """Prefix uploads so identical filenames never collide in the vault."""
        return f"{uuid4().hex}_{display_name}"

    @staticmethod
    def options() -> tuple[str | None, str | None]:
        """Return the password and document type chosen for this batch, if any."""
        doc_type = st.session_state.get("upload_doc_type")
        return st.session_state.get("upload_password") or None, None if doc_type == "auto" else doc_type

    def _post_with_progress(
        self, display_name: str, content: bytes, progress, progress_value: float
    ) -> dict:
        """Keep progress feedback alive during the blocking parse and extraction request."""
        encoded = base64.b64encode(content).decode("ascii")
        password, doc_type = self.options()
        started_at = time.monotonic()
        tick = 0
        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(
                self.api.upload_document, self.stored_name(display_name), encoded, password, doc_type
            )
            while not request.done():
                elapsed = int(time.monotonic() - started_at)
                dots = "." * ((tick % 3) + 1)
                progress.progress(
                    progress_value,
                    text=(
                        f"Reading and extracting {display_name}. "
                        f"Time: {elapsed}s elapsed{dots} "
                    ),
                )
                tick += 1
                time.sleep(self.PROGRESS_TICK_SECONDS)
            return request.result()

    @staticmethod
    def _record_success(upload_id: str, metadata: dict) -> None:
        st.session_state.saved_uploads.add(upload_id)
        st.session_state.uploaded_documents.append({"upload_id": upload_id, **metadata})

    def upload_documents(self, uploads: list) -> None:
        """Upload files not yet saved this session, tracking overall progress."""
        pending = []
        for upload in uploads:
            content = upload.getbuffer()
            upload_id = sha256(content).hexdigest()
            if upload_id not in st.session_state.saved_uploads:
                pending.append((upload, upload_id, content))
        if not pending:
            return

        progress = st.progress(0.0, text="Preparing documents for upload...")
        total_bytes = sum(len(content) for _, _, content in pending)
        written_bytes = 0
        try:
            for upload, upload_id, content in pending:
                display_name = Path(upload.name).name
                fraction = written_bytes / total_bytes if total_bytes else 0.0
                progress.progress(
                    fraction,
                    text=(
                        f"Reading {display_name} with Docling and the local model. "
                        "The first request may include model initialization."
                    ),
                )
                try:
                    metadata = self._post_with_progress(display_name, content, progress, fraction)
                except httpx.HTTPError as error:
                    st.session_state.failed_uploads[upload_id] = {
                        "name": display_name,
                        "content": bytes(content),
                    }
                    message = LunaLexiaAPI.error_message(error, str(error))
                    st.error(f"Could not upload {display_name}: {message}")
                    written_bytes += len(content)
                    continue
                written_bytes += len(content)
                progress.progress(
                    written_bytes / total_bytes if total_bytes else 1.0,
                    text=f"Extraction and upload complete for {display_name}",
                )
                self._record_success(upload_id, metadata)
        finally:
            progress.empty()

    def retry_upload(self, upload_id: str) -> None:
        """Retry one previously failed upload."""
        failed = st.session_state.failed_uploads[upload_id]
        progress = st.progress(0.0, text="Preparing document for analysis...")
        try:
            metadata = self._post_with_progress(failed["name"], failed["content"], progress, 0.0)
            progress.progress(1.0, text=f"Analysis complete for {failed['name']}.")
            self._record_success(upload_id, metadata)
            del st.session_state.failed_uploads[upload_id]
        finally:
            progress.empty()

    @staticmethod
    def clear_results() -> None:
        """Reset upload results and recreate the uploader widget."""
        st.session_state.uploaded_documents = []
        st.session_state.saved_uploads = set()
        st.session_state.uploader_key += 1

    def render(self) -> None:
        password_column, type_column = st.columns(2)
        password_column.text_input(
            "PDF password (if protected)",
            type="password",
            key="upload_password",
            help="Indian payslips and statements are often protected with your PAN or date of birth.",
        )
        types = self.context.types
        type_column.selectbox(
            "Document type",
            options=["auto", *types.keys()],
            format_func=lambda key: "Detect automatically" if key == "auto" else types.label(key),
            key="upload_doc_type",
        )
        uploads = st.file_uploader(
            ":material/upload_file: Drag and drop PDFs or photos of documents",
            type=list(self.context.config.UPLOAD_TYPES),
            accept_multiple_files=True,
            key=f"document_uploader_{st.session_state.uploader_key}",
        )
        if uploads:
            self.upload_documents(uploads)
        self._render_retries()
        self._render_results()

    def _render_retries(self) -> None:
        for upload_id, failed in st.session_state.failed_uploads.copy().items():
            if st.button(f"Retry {failed['name']}", key=f"retry_{upload_id}"):
                try:
                    self.retry_upload(upload_id)
                    st.rerun()
                except httpx.HTTPError as error:
                    message = LunaLexiaAPI.error_message(error, str(error))
                    st.error(f"Could not upload {failed['name']}: {message}")

    def _render_results(self) -> None:
        types = self.context.types
        for document in st.session_state.uploaded_documents:
            with st.container(border=True):
                DocumentLabel.render(st, document["name"])
                if document.get("status") == "duplicate":
                    st.info(document.get("message", "This document is already stored."))
                facts = [
                    types.label(document.get("doc_type")),
                    f"FY {document['financial_year']}" if document.get("financial_year") else None,
                    document.get("issuer"),
                ]
                st.markdown(f"**{document['title']}** · {' · '.join(fact for fact in facts if fact)}")
                st.write(document["summary"])
                st.caption("  ".join(f"#{tag}" for tag in document["tags"]))
                for flag in document.get("review_flags") or []:
                    st.warning(flag, icon=":material/fact_check:")
        if st.session_state.uploaded_documents and st.button(
            "Clear", icon=":material/refresh:", width="stretch"
        ):
            self.clear_results()
            st.rerun()
