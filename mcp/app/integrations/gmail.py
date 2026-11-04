"""Read-only Gmail access: the OAuth sign-in and a client that finds and downloads attachments."""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from app.integrations.drive import DOWNLOADABLE, DriveOAuth, renewing_credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
# Mail often labels a PDF application/octet-stream, so its extension counts too.
IMPORTABLE_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".webp", ".docx", ".doc", ".xlsx", ".xls"})
HTML_NOISE = re.compile(r"<(style|script)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
HTML_TAG = re.compile(r"<[^>]+>")
SPACES = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES = re.compile(r"\n\s*\n+")


class GmailSignInExpired(ValueError):
    """Google no longer accepts the saved sign-in; the person has to connect Gmail again."""

    def __init__(self, message: str = "Your Gmail sign-in has expired. Reconnect Gmail from Lexia's upload dialog to continue; your documents are safe.") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class GmailAttachment:
    """One attached file. Gmail's attachment IDs change between reads, but part IDs do not."""

    message_id: str
    part_id: str
    name: str
    subject: str = ""
    sender: str = ""
    date: str = ""

    @property
    def source_id(self) -> str:
        return f"{self.message_id}:{self.part_id}"

    @property
    def upload_name(self) -> str:
        """Monthly statements often share a filename, so the mail's date tells them apart."""
        return f"{self.date}_{self.name}" if self.date else self.name

    def summary(self) -> dict[str, str]:
        return {"source_id": self.source_id, "name": self.name, "subject": self.subject, "sender": self.sender, "date": self.date}


@dataclass(frozen=True)
class GmailDownload:
    """An attachment's bytes and the text of the mail it came with."""

    attachment: GmailAttachment
    data: bytes
    text: str


class GmailOAuth(DriveOAuth):
    """The one-time read-only Gmail sign-in; it can share Drive's Google client."""

    SCOPES = SCOPES
    SALT = "lexia-gmail"
    PURPOSE = "gmail"
    LABEL = "Gmail"

    def account_email(self, credentials) -> str:
        return GmailClient.from_credentials(credentials).account_email()


def decoded(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def parts_of(part: dict) -> Iterator[dict]:
    """A message's MIME parts, however deeply they nest."""
    yield part
    for child in part.get("parts") or []:
        yield from parts_of(child)


def headers_of(part: dict) -> dict[str, str]:
    return {header["name"].lower(): header["value"] for header in part.get("headers") or []}


def is_attachment(part: dict) -> bool:
    """A file someone attached: not a logo or tracking image embedded in the mail."""
    name = part.get("filename") or ""
    mime_type = part.get("mimeType") or ""
    body = part.get("body") or {}
    if not name or not (body.get("attachmentId") or body.get("data")):
        return False
    if mime_type not in DOWNLOADABLE and PurePosixPath(name).suffix.lower() not in IMPORTABLE_EXTENSIONS:
        return False
    headers = headers_of(part)
    embedded = "content-id" in headers or headers.get("content-disposition", "").lower().startswith("inline")
    return not (mime_type.startswith("image/") and embedded)


def attachments_of(message: dict) -> list[GmailAttachment]:
    payload = message.get("payload") or {}
    headers = headers_of(payload)
    sent = datetime.fromtimestamp(int(message["internalDate"]) / 1000, timezone.utc).date().isoformat() if message.get("internalDate") else ""
    return [
        GmailAttachment(message["id"], part.get("partId") or "", part["filename"], headers.get("subject", ""), headers.get("from", ""), sent)
        for part in parts_of(payload)
        if is_attachment(part)
    ]


def text_of(message: dict) -> str:
    """The mail's text, from its plain part or else its HTML, where banks print password hints."""
    found: dict[str, str] = {}
    for part in parts_of(message.get("payload") or {}):
        mime_type, data = part.get("mimeType"), (part.get("body") or {}).get("data")
        if mime_type in ("text/plain", "text/html") and data and not part.get("filename"):
            found[mime_type] = found.get(mime_type, "") + decoded(data).decode(errors="replace")
    if found.get("text/plain", "").strip():
        return found["text/plain"]
    text = HTML_TAG.sub("\n", HTML_NOISE.sub(" ", found.get("text/html", "")))
    return BLANK_LINES.sub("\n", SPACES.sub(" ", html.unescape(text))).strip() or message.get("snippet", "")


class GmailClient:
    """Search one connected mailbox and download its attachments."""

    def __init__(self, service) -> None:
        self.service = service

    @classmethod
    def from_credentials(cls, credentials) -> GmailClient:
        from googleapiclient.discovery import build

        return cls(build("gmail", "v1", credentials=credentials, cache_discovery=False))

    @classmethod
    def from_refresh_token(cls, refresh_token: str, client_id: str, client_secret: str) -> GmailClient:
        return cls.from_credentials(
            renewing_credentials(refresh_token, client_id, client_secret, SCOPES, GmailSignInExpired)
        )

    def account_email(self) -> str:
        return self.service.users().getProfile(userId="me").execute()["emailAddress"]

    def _message(self, message_id: str) -> dict:
        return self.service.users().messages().get(userId="me", id=message_id, format="full").execute()

    def search(self, query: str) -> Iterator[GmailAttachment]:
        """Every importable attachment of the mails matching a Gmail search, newest first."""
        token = None
        while True:
            response = (
                self.service.users().messages().list(userId="me", q=f"({query}) has:attachment", maxResults=100, pageToken=token).execute()
            )
            for reference in response.get("messages", []):
                yield from attachments_of(self._message(reference["id"]))
            token = response.get("nextPageToken")
            if not token:
                return

    def download(self, source_id: str) -> GmailDownload:
        """Fetch ``message_id:part_id``, reading the message again for a current attachment ID."""
        message_id, _, part_id = source_id.partition(":")
        message = self._message(message_id)
        attachment = next((item for item in attachments_of(message) if item.part_id == part_id), None)
        if attachment is None:
            raise FileNotFoundError("This attachment is no longer in that mail.")
        part = next(item for item in parts_of(message["payload"]) if item.get("partId") == part_id)
        body = part["body"]
        data = body.get("data") or (
            self.service.users().messages().attachments().get(userId="me", messageId=message_id, id=body["attachmentId"]).execute()["data"]
        )
        return GmailDownload(attachment, decoded(data), text_of(message))
