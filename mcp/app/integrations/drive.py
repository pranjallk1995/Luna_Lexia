"""Read-only Google Drive access: the OAuth sign-in and a client that lists and downloads files."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from itsdangerous import BadSignature, URLSafeTimedSerializer

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_TYPE = "application/vnd.google-apps.folder"
# Google Docs, Sheets, and Slides have no file bytes; Drive exports them as PDF.
EXPORT_AS_PDF = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.google-apps.presentation",
    }
)
DOWNLOADABLE = frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"})
FILE_FIELDS = "id, name, mimeType, md5Checksum, size, modifiedTime"
FOLDER_LINK = re.compile(r"/folders/([A-Za-z0-9_-]{10,})")
DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{25,}")


@dataclass(frozen=True)
class DriveFile:
    """One Drive file and where it sits under the imported folder."""

    file_id: str
    name: str
    mime_type: str
    md5: str | None = None
    size: int | None = None
    modified: str | None = None
    path: str = ""

    @property
    def version(self) -> str | None:
        """What identifies this version: the checksum, or for Google files the edit time."""
        return self.md5 or (f"modified:{self.modified}" if self.modified else None)

    @property
    def importable(self) -> bool:
        return self.mime_type in DOWNLOADABLE or self.mime_type in EXPORT_AS_PDF

    @property
    def upload_name(self) -> str:
        """Exported Google files become PDFs, so their name gets a .pdf extension."""
        return f"{self.name}.pdf" if self.mime_type in EXPORT_AS_PDF else self.name

    @classmethod
    def from_api(cls, item: dict, path: str = "") -> DriveFile:
        return cls(
            file_id=item["id"],
            name=item["name"],
            mime_type=item["mimeType"],
            md5=item.get("md5Checksum"),
            size=int(item["size"]) if item.get("size") else None,
            modified=item.get("modifiedTime"),
            path=path,
        )


class DriveOAuth:
    """The one-time read-only Drive sign-in, run by Lexia's backend callback.

    The ``state`` round-trip is signed with the API token so only a sign-in that
    Lexia started can complete. PKCE is off because the client is confidential
    (it holds a secret) and the callback runs in a different request than the start.
    """

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, state_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.signer = URLSafeTimedSerializer(state_secret, salt="lexia-drive")
        if urlparse(redirect_uri).hostname in {"localhost", "127.0.0.1"}:
            # oauthlib refuses plain-HTTP callbacks unless told this is local development.
            os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def flow(self, state: str | None = None):
        from google_auth_oauthlib.flow import Flow

        config = {
            "web": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        return Flow.from_client_config(
            config, scopes=SCOPES, redirect_uri=self.redirect_uri, state=state, autogenerate_code_verifier=False
        )

    def authorization_url(self) -> str:
        if not self.configured:
            raise ValueError("Google Drive is not configured; set DRIVE_CLIENT_ID and DRIVE_CLIENT_SECRET.")
        state = self.signer.dumps({"purpose": "drive"})
        url, _ = self.flow(state).authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent", state=state
        )
        return url

    def exchange(self, callback_url: str, state: str) -> tuple[str, str]:
        """Finish the sign-in; return the account email and its refresh token."""
        try:
            self.signer.loads(state, max_age=900)
        except BadSignature as error:
            raise PermissionError("This Google Drive sign-in link expired or was not started by Lexia.") from error
        flow = self.flow(state)
        flow.fetch_token(authorization_response=callback_url)
        credentials = flow.credentials
        if not credentials.refresh_token:
            raise ValueError("Google did not return a refresh token; remove Lexia's access and connect again.")
        email = DriveClient.from_credentials(credentials).account_email()
        return email, credentials.refresh_token


class DriveClient:
    """List folders and download files from one connected Drive account."""

    def __init__(self, service) -> None:
        self.service = service

    @classmethod
    def from_credentials(cls, credentials) -> DriveClient:
        from googleapiclient.discovery import build

        return cls(build("drive", "v3", credentials=credentials, cache_discovery=False))

    @classmethod
    def from_refresh_token(cls, refresh_token: str, client_id: str, client_secret: str) -> DriveClient:
        from google.oauth2.credentials import Credentials

        credentials = Credentials(
            None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        return cls.from_credentials(credentials)

    def account_email(self) -> str:
        return self.service.about().get(fields="user(emailAddress)").execute()["user"]["emailAddress"]

    def _list(self, query: str, fields: str = FILE_FIELDS) -> list[dict]:
        items, token = [], None
        while True:
            response = (
                self.service.files()
                .list(q=query, fields=f"nextPageToken, files({fields}, parents)", pageSize=1000, pageToken=token,
                      orderBy="folder,name")
                .execute()
            )
            items.extend(response.get("files", []))
            token = response.get("nextPageToken")
            if not token:
                return items

    def folders(self, parent_id: str | None = None) -> list[dict]:
        """Subfolders of ``parent_id``, or of My Drive's root."""
        parent = parent_id or "root"
        return [
            {"folder_id": item["id"], "name": item["name"]}
            for item in self._list(f"'{parent}' in parents and mimeType = '{FOLDER_TYPE}' and trashed = false")
        ]

    def resolve_folder(self, spec: str) -> dict:
        """Accept a folder link, ID, or exact name; reject a name several folders share."""
        spec = spec.strip()
        match = FOLDER_LINK.search(spec)
        if match or DRIVE_ID.fullmatch(spec):
            folder = self.service.files().get(fileId=match[1] if match else spec, fields="id, name, mimeType").execute()
            if folder["mimeType"] != FOLDER_TYPE:
                raise ValueError(f"{folder['name']!r} is a file, not a folder.")
            return {"folder_id": folder["id"], "name": folder["name"]}
        escaped = spec.replace("\\", "\\\\").replace("'", "\\'")
        matches = self._list(f"name = '{escaped}' and mimeType = '{FOLDER_TYPE}' and trashed = false", "id, name")
        if not matches:
            raise FileNotFoundError(f"No Drive folder is named {spec!r}.")
        if len(matches) > 1:
            parents = [self._parent_name(item) for item in matches]
            choices = "; ".join(f"{item['id']} (inside {parent})" for item, parent in zip(matches, parents))
            raise ValueError(f"{len(matches)} folders are named {spec!r}; pass one folder ID instead: {choices}.")
        return {"folder_id": matches[0]["id"], "name": matches[0]["name"]}

    def _parent_name(self, item: dict) -> str:
        parents = item.get("parents") or []
        if not parents:
            return "My Drive"
        return self.service.files().get(fileId=parents[0], fields="name").execute().get("name", "My Drive")

    def walk(self, folder_id: str, recursive: bool = True, path: str = "") -> list[DriveFile]:
        """Every file under the folder (including unsupported ones), with its subfolder path."""
        files = []
        for item in self._list(f"'{folder_id}' in parents and trashed = false"):
            if item["mimeType"] == FOLDER_TYPE:
                if recursive:
                    files.extend(self.walk(item["id"], True, f"{path}{item['name']}/"))
            else:
                files.append(DriveFile.from_api(item, path))
        return files

    def file(self, file_id: str) -> DriveFile:
        return DriveFile.from_api(self.service.files().get(fileId=file_id, fields=FILE_FIELDS).execute())

    def download(self, file: DriveFile) -> bytes:
        if file.mime_type in EXPORT_AS_PDF:
            return self.service.files().export(fileId=file.file_id, mimeType="application/pdf").execute()
        return self.service.files().get_media(fileId=file.file_id).execute()
