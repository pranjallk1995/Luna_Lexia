"""Saved passwords of locked PDFs, tried on every locked PDF before asking for one.

Banks and employers lock each month's statement or payslip with the same password, so the
person saves it once ("HDFC card statement", for credit_card_statement) and a locked PDF
of that kind opens by itself. A locked PDF is tried with the passwords saved for its
document type first, when the caller says what it is (ChatGPT's doc_type), then with every
other saved password; the ones that worked lately go first. Passwords are encrypted with
the same key as the Google sign-ins, and listed only by their labels. The UI can show or
change one only after its gateway has checked the person's Luna PIN; the MCP tools never show one.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.schemas import document_type
from app.data.repositories import PasswordRepository

logger = logging.getLogger(__name__)

# The saved password that opened the last locked PDF in this request or job, for its reply.
opened_with: ContextVar[dict[str, Any] | None] = ContextVar("opened_with", default=None)


class SavedPasswords:
    MAX_LABEL = 80
    MAX_PASSWORD = 256

    def __init__(self, repository: PasswordRepository, encryption_key: str) -> None:
        self.repository = repository
        self.cipher = Fernet(encryption_key.encode()) if encryption_key else None

    def checked(self, label: str, password: str, doc_type: str | None, *, keep_password: bool = False) -> tuple[str, str]:
        """The tidied label and password, or a ValueError saying what is wrong with them."""
        if self.cipher is None:
            raise ValueError("Set LUNALEXIA_TOKEN_ENCRYPTION_KEY (or the API token) before saving passwords.")
        label, password = " ".join((label or "").split())[: self.MAX_LABEL], password or ""
        if not label:
            raise ValueError("Give the password a label, such as 'HDFC card statement'.")
        if (not password and not keep_password) or len(password) > self.MAX_PASSWORD:
            raise ValueError(f"The password must be 1 to {self.MAX_PASSWORD} characters.")
        if doc_type:
            document_type(doc_type)
        return label, password

    def encrypt(self, password: str) -> str:
        return self.cipher.encrypt(password.encode()).decode()

    def save(self, label: str, password: str, doc_type: str | None = None) -> dict[str, Any]:
        label, password = self.checked(label, password, doc_type)
        return {"status": "saved", **self.repository.add(label, doc_type or None, self.encrypt(password))}

    def update(self, password_id: int, label: str, password: str | None = None, doc_type: str | None = None) -> dict[str, Any]:
        """Rename a saved password, change its document type, and, when ``password`` is given, the password."""
        label, password = self.checked(label, password or "", doc_type, keep_password=True)
        row = self.repository.update(password_id, label, doc_type or None, self.encrypt(password) if password else None)
        if row is None:
            raise FileNotFoundError(f"No saved password has id {password_id}.")
        return {"status": "updated", **row}

    def reveal(self, password_id: int) -> dict[str, Any]:
        """The saved password itself, for its owner once the UI gateway has checked their PIN."""
        if self.cipher is None:
            raise ValueError("Set LUNALEXIA_TOKEN_ENCRYPTION_KEY (or the API token) before saving passwords.")
        row = self.repository.secret(password_id)
        if row is None:
            raise FileNotFoundError(f"No saved password has id {password_id}.")
        try:
            password = self.cipher.decrypt(row["encrypted_password"].encode()).decode()
        except InvalidToken:
            raise ValueError("This password was saved under another key and cannot be read. Save it again.") from None
        return {"password_id": row["password_id"], "label": row["label"], "doc_type": row["doc_type"], "password": password}

    def listing(self) -> dict[str, Any]:
        rows = self.repository.listing()
        return {"passwords": rows, "count": len(rows)}

    def forget(self, password_id: int) -> dict[str, Any]:
        if not self.repository.delete(password_id):
            raise FileNotFoundError(f"No saved password has id {password_id}.")
        return {"status": "forgotten", "password_id": password_id}

    def candidates(self, doc_type: str | None = None) -> list[tuple[dict[str, Any], str]]:
        """Each saved password to try, with its row: those of ``doc_type`` first, then the rest."""
        if self.cipher is None:
            return []
        rows = self.repository.to_try()
        ordered = [row for row in rows if doc_type and row["doc_type"] == doc_type]
        ordered += [row for row in rows if row not in ordered]
        tries = []
        for row in ordered:
            try:
                password = self.cipher.decrypt(row["encrypted_password"].encode()).decode()
            except InvalidToken:
                # Saved under another key (the API token changed): it cannot be read any more.
                logger.warning("Saved password %s cannot be decrypted", row["password_id"])
                continue
            tries.append(({key: row[key] for key in ("password_id", "label", "doc_type")}, password))
        return tries

    def worked(self, row: dict[str, Any]) -> None:
        self.repository.worked(row["password_id"])
        opened_with.set(row)
