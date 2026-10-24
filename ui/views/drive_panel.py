"""Google Drive import: connect once, import folders as background jobs, and follow their progress."""

import httpx
import pandas as pd
import streamlit as st

from services.api import LunaLexiaAPI
from views.shared import LexiaTab


class DriveImportPanel(LexiaTab):
    """Connect Google Drive, start folder imports, and retry files that need a password."""

    OUTCOME_ICONS = {
        "imported": "✅",
        "duplicate": "♻️",
        "skipped": "⏭️",
        "needs_password": "🔒",
        "unsupported": "➖",
        "failed": "⚠️",
    }

    def render(self) -> None:
        try:
            status = self.api.drive_status()
        except httpx.HTTPError as error:
            st.error(LunaLexiaAPI.error_message(error, "Could not reach Lexia."))
            return
        if not status["configured"]:
            st.info(
                "Google Drive is not set up yet: Lexia's backend could not read the Google "
                "client ID and secret from ui/.streamlit/secrets.toml.",
                icon=":material/settings:",
            )
            return
        if not status["connected"]:
            self._render_connect()
            return
        self._render_account(status["email"])
        self._render_import()
        self._render_jobs()

    def _render_connect(self) -> None:
        try:
            url = self.api.drive_authorization_url()
        except httpx.HTTPError as error:
            st.error(LunaLexiaAPI.error_message(error, "Could not start the Google sign-in."))
            return
        st.link_button("Connect Google Drive", url, icon=":material/add_to_drive:", width="stretch")
        st.caption("Read-only access. After signing in, close that tab and reopen this dialog.")

    def _render_account(self, email: str) -> None:
        text_column, button_column = st.columns([8, 1], vertical_alignment="center")
        text_column.caption(f":material/cloud_done: Connected as {email}")
        if button_column.button(":material/link_off:", help="Disconnect Google Drive", key="drive_disconnect", width="stretch"):
            self.api.drive_disconnect()
            st.session_state.pop("drive_root_folders", None)
            st.rerun(scope="fragment")

    def _render_import(self) -> None:
        if "drive_root_folders" not in st.session_state:
            try:
                st.session_state.drive_root_folders = self.api.drive_folders()
            except httpx.HTTPError as error:
                st.error(LunaLexiaAPI.error_message(error, "Could not list your Drive folders."))
        folders = st.session_state.get("drive_root_folders", [])
        names = {folder["folder_id"]: folder["name"] for folder in folders}
        picked = st.selectbox(
            "Folder in My Drive",
            options=["", *names],
            format_func=lambda folder_id: names.get(folder_id, "Choose a folder in My Drive"),
            key="drive_folder_pick",
            label_visibility="collapsed",
        )
        typed = st.text_input(
            "Or a folder link, ID, or name",
            key="drive_folder_text",
            placeholder="…or paste a folder link, ID, or exact name",
            label_visibility="collapsed",
        ).strip()
        types = self.context.types
        options_column, type_column, button_column = st.columns([3, 4, 1], vertical_alignment="center")
        recursive = options_column.toggle("Subfolders", value=True, key="drive_recursive")
        doc_type = type_column.selectbox(
            "Document type",
            options=["auto", *types.keys()],
            format_func=lambda key: "Detect each type" if key == "auto" else types.label(key),
            key="drive_doc_type",
            label_visibility="collapsed",
        )
        folder = typed or picked
        if button_column.button(
            ":material/cloud_download:",
            help="Import every PDF and image in this folder",
            key="drive_import",
            disabled=not folder,
            width="stretch",
        ):
            try:
                job = self.api.import_drive_folder(folder, recursive, None if doc_type == "auto" else doc_type)
            except httpx.HTTPError as error:
                st.error(LunaLexiaAPI.error_message(error, "Could not start the import."))
                return
            st.toast(f"Importing {job['source']}", icon=":material/cloud_download:")

    def _render_jobs(self) -> None:
        heading_column, refresh_column = st.columns([8, 1], vertical_alignment="center")
        heading_column.markdown('<div class="lv-preview-heading">Recent imports</div>', unsafe_allow_html=True)
        refresh_column.button(":material/refresh:", help="Refresh progress", key="drive_jobs_refresh", width="stretch")
        try:
            jobs = self.api.import_jobs(5)
        except httpx.HTTPError:
            st.caption("Import progress is unavailable right now.")
            return
        if not jobs:
            st.caption("No imports yet.")
            return
        for job in jobs:
            self._render_job(job)

    def _render_job(self, job: dict) -> None:
        total, processed = job.get("total") or 0, job.get("processed") or 0
        counts = " · ".join(
            f"{self.OUTCOME_ICONS.get(outcome, '')} {count} {outcome.replace('_', ' ')}"
            for outcome, count in sorted((job.get("outcomes") or {}).items())
        )
        with st.expander(f"{job['source']} — {job['status']} ({processed}/{total})"):
            if job["status"] in ("queued", "listing", "running") and total:
                st.progress(min(processed / total, 1.0))
            if job.get("error"):
                st.error(job["error"])
            if counts:
                st.caption(counts)
            report = self.api.import_job(str(job["job_id"]))
            items = report.get("items") or []
            if items:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "": self.OUTCOME_ICONS.get(item["outcome"], ""),
                                "File": f"{item.get('path') or ''}{item['name']}",
                                "Outcome": item["outcome"].replace("_", " "),
                                "Details": item.get("error") or item.get("document_name") or "",
                            }
                            for item in items
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            self._render_password_retry(job, items)

    def _render_password_retry(self, job: dict, items: list[dict]) -> None:
        locked = [item for item in items if item["outcome"] == "needs_password" and item.get("source_id")]
        if not locked:
            return
        password_column, button_column = st.columns([8, 1], vertical_alignment="center")
        password = password_column.text_input(
            "Password for the locked PDFs",
            type="password",
            key=f"drive_password_{job['job_id']}",
            placeholder=f"Password for the {len(locked)} locked PDF(s)",
            label_visibility="collapsed",
        )
        if button_column.button(
            ":material/lock_open:",
            help="Retry the locked PDFs with this password",
            key=f"drive_retry_{job['job_id']}",
            disabled=not password,
            width="stretch",
        ):
            try:
                self.api.upload_batch([{"drive_file_id": item["source_id"], "password": password} for item in locked])
            except httpx.HTTPError as error:
                st.error(LunaLexiaAPI.error_message(error, "Could not retry the locked PDFs."))
                return
            st.toast(f"Retrying {len(locked)} locked PDF(s)", icon=":material/lock_open:")
