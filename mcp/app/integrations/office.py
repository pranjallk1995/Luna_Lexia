"""Convert Word and Excel files to PDF with headless LibreOffice."""

import hashlib
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from app.core.config import AppConfig


class OfficeConverter:
    """Turn DOCX, DOC, XLSX, and XLS bytes into PDF bytes.

    Lexia stores the PDF, like a Google Doc exported from Drive, so pages can be
    rendered, previewed, and shown to the vision model like any other document.
    Each sheet of a workbook becomes one page, so wide sheets are not split
    across pages. The output is reproducible: the creation date is pinned by
    ``SOURCE_DATE_EPOCH`` (and, where Calc ignores it, rewritten) and the random
    PDF ID and checksum are replaced with a hash of the source, so converting the same file
    twice gives identical bytes and duplicate detection keeps working.

    LibreOffice cannot share one profile between processes, so conversions run
    one at a time.
    """

    EXTENSIONS = {"DOCX": ".docx", "DOC": ".doc", "XLSX": ".xlsx", "XLS": ".xls"}
    SPREADSHEETS = frozenset({"XLSX", "XLS"})
    WRITER_FILTER = "pdf:writer_pdf_Export"
    CALC_FILTER = 'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
    PDF_ID = re.compile(rb"/ID\s*\[\s*<[0-9A-Fa-f]{32}>\s*<[0-9A-Fa-f]{32}>\s*\]")
    PDF_DATE = re.compile(rb"(/(?:CreationDate|ModDate)\s*\(D:)\d{14}")
    PDF_CHECKSUM = re.compile(rb"(/DocChecksum\s*/)[0-9A-Fa-f]{32}")

    def __init__(self, profile_dir: Path = Path(tempfile.gettempdir()) / "lexia-libreoffice") -> None:
        self.profile_dir = profile_dir
        self._lock = threading.Lock()

    def to_pdf(self, data: bytes, file_format: str) -> bytes:
        """Convert ``data`` (a format in ``EXTENSIONS``) to PDF bytes."""
        output_filter = self.CALC_FILTER if file_format in self.SPREADSHEETS else self.WRITER_FILTER
        with self._lock, tempfile.TemporaryDirectory(prefix="lexia-office-") as workdir:
            source = Path(workdir) / f"document{self.EXTENSIONS[file_format]}"
            source.write_bytes(data)
            command = [
                AppConfig.OFFICE_BINARY,
                f"-env:UserInstallation={self.profile_dir.as_uri()}",
                "--headless",
                "--norestore",
                "--convert-to",
                output_filter,
                "--outdir",
                workdir,
                str(source),
            ]
            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    timeout=AppConfig.OFFICE_TIMEOUT_SECONDS,
                    env={**os.environ, "SOURCE_DATE_EPOCH": "0"},
                )
            except FileNotFoundError as error:
                raise ValueError("Word and Excel files need LibreOffice, which is not installed.") from error
            except subprocess.TimeoutExpired as error:
                raise ValueError(f"Converting the {file_format} file to PDF took too long.") from error
            except subprocess.CalledProcessError as error:
                raise ValueError(f"The {file_format} file could not be converted to PDF.") from error
            output = source.with_suffix(".pdf")
            if not output.is_file() or not output.stat().st_size:
                raise ValueError(f"The {file_format} file could not be converted to PDF.")
            return self._reproducible(output.read_bytes(), data)

    @classmethod
    def _reproducible(cls, pdf: bytes, source: bytes) -> bytes:
        """Pin the dates and derive the ID and checksum from the source, keeping every length the same.

        Equal lengths keep the cross-reference offsets valid, since the
        document information dictionary can come before the xref table.
        """
        digest = hashlib.md5(source, usedforsecurity=False).hexdigest().upper().encode()
        pdf = cls.PDF_DATE.sub(rb"\g<1>19700101000000", pdf)
        pdf = cls.PDF_CHECKSUM.sub(rb"\g<1>" + digest, pdf)
        return cls.PDF_ID.sub(b"/ID [ <" + digest + b"> <" + digest + b"> ]", pdf)
