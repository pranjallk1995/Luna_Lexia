"""UI-facing client for Luna Lexia's HTTP routes."""

from typing import Any
from urllib.parse import quote

import httpx

from config import UIConfig


class LunaLexiaAPI:
    """Keep backend routes and HTTP details out of Streamlit views."""

    def __init__(self, config: UIConfig) -> None:
        self.base_url = config.api_url
        self.headers = {"Authorization": f"Bearer {config.api_token}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> httpx.Response:
        response = httpx.request(
            method, f"{self.base_url}{path}", headers=self.headers, timeout=timeout, **kwargs
        )
        response.raise_for_status()
        return response

    @staticmethod
    def error_message(error: httpx.HTTPError, fallback: str) -> str:
        """Prefer the backend's ``error`` text over a generic message."""
        if isinstance(error, httpx.HTTPStatusError):
            try:
                return error.response.json().get("error", fallback)
            except ValueError:
                return fallback
        return fallback

    @staticmethod
    def metadata_change(
        title: str | None = None,
        summary: str | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        extracted: dict[str, Any] | None = None,
        mark_reviewed: bool = False,
    ) -> dict[str, Any]:
        """Build a metadata-edit payload containing only the requested changes."""
        change = {
            "title": title,
            "summary": summary,
            "add_tags": add_tags,
            "remove_tags": remove_tags,
            "extracted": extracted,
            "mark_reviewed": mark_reviewed or None,
        }
        return {key: value for key, value in change.items() if value is not None}

    @staticmethod
    def document_path(prefix: str, name: str, suffix: str = "") -> str:
        return f"{prefix}/{quote(name, safe='')}{suffix}"

    def document_types(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/document-types", timeout=10.0).json()["document_types"]

    # Visible documents

    def metadata(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/metadata", timeout=10.0).json()["documents"]

    def upload_document(
        self, name: str, content_base64: str, password: str | None = None, doc_type: str | None = None
    ) -> dict[str, Any]:
        payload = {"name": name, "content_base64": content_base64, "password": password, "doc_type": doc_type}
        return self.request(
            "POST",
            "/api/documents",
            json={key: value for key, value in payload.items() if value},
            timeout=900.0,
        ).json()

    def delete_document(self, name: str) -> None:
        self.request("DELETE", self.document_path("/api/documents", name))

    def rename_document(self, name: str, new_name: str) -> dict[str, Any]:
        return self.request(
            "POST", self.document_path("/api/documents", name, "/rename"), json={"new_name": new_name}
        ).json()

    def update_document_metadata(self, name: str, payload: dict) -> dict[str, Any]:
        return self.request(
            "POST", self.document_path("/api/documents", name, "/metadata"), json=payload
        ).json()

    def reextract_document(self, name: str, doc_type: str | None) -> dict[str, Any]:
        return self.request(
            "POST",
            self.document_path("/api/documents", name, "/reextract"),
            json={"doc_type": doc_type},
            timeout=900.0,
        ).json()

    def favorites(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/favorites", timeout=10.0).json()["documents"]

    def set_favorites(self, names: list[str], favorite: bool) -> None:
        self.request("POST", "/api/favorites", json={"names": names, "favorite": favorite})

    # Google Drive and bulk imports

    def drive_status(self) -> dict[str, Any]:
        return self.request("GET", "/api/drive/status", timeout=10.0).json()

    def drive_authorization_url(self) -> str:
        return self.request("GET", "/api/drive/authorization-url", timeout=10.0).json()["url"]

    def drive_disconnect(self) -> None:
        self.request("POST", "/api/drive/disconnect")

    def drive_folders(self, parent_id: str | None = None) -> list[dict[str, Any]]:
        params = {"parent": parent_id} if parent_id else None
        return self.request("GET", "/api/drive/folders", params=params, timeout=30.0).json()["folders"]

    def import_drive_folder(self, folder: str, recursive: bool, doc_type: str | None) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/imports/drive-folder",
            json={"folder": folder, "recursive": recursive, "doc_type": doc_type},
            timeout=60.0,
        ).json()

    def upload_batch(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self.request("POST", "/api/imports/batch", json={"items": items}, timeout=60.0).json()

    def import_jobs(self, limit: int = 5) -> list[dict[str, Any]]:
        return self.request("GET", "/api/imports", params={"limit": limit}, timeout=10.0).json()["jobs"]

    def import_job(self, job_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/imports/{job_id}", timeout=10.0).json()

    # Hidden documents

    def hidden_documents(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/hidden/documents", timeout=10.0).json()["documents"]

    def hide_documents(self, names: list[str]) -> None:
        self.request("POST", "/api/hidden/hide", json={"names": names})

    def restore_hidden_documents(self, names: list[str]) -> None:
        self.request("POST", "/api/hidden/restore", json={"names": names})

    def delete_hidden_documents(self, names: list[str]) -> None:
        self.request("POST", "/api/hidden/delete", json={"names": names})

    def update_hidden_metadata(self, name: str, payload: dict) -> dict[str, Any]:
        return self.request(
            "POST", self.document_path("/api/hidden/documents", name, "/metadata"), json=payload
        ).json()

    def reextract_hidden_document(self, name: str, doc_type: str | None) -> dict[str, Any]:
        return self.request(
            "POST",
            self.document_path("/api/hidden/documents", name, "/reextract"),
            json={"doc_type": doc_type},
            timeout=900.0,
        ).json()
