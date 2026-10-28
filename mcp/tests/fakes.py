"""In-memory test doubles for Luna Lexia repositories, parsing, and analysis."""

import base64
import hashlib
import json
import sys
import zipfile
from dataclasses import asdict
from io import BytesIO
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1]))

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from app.core.models import DocumentClassification, DocumentContent
from app.core.schemas import Payslip
from app.data.repositories import PUBLIC_DOCUMENT_PATH
from app.services.documents import DocumentReader

PAYSLIP = {
    "employer_name": "Acme Corp",
    "employee_name": "Asha Rao",
    "pan": "ABCDE1234F",
    "pay_period": "2025-03",
    "pay_date": "2025-03-31",
    "gross_earnings": 100000,
    "total_deductions": 20000,
    "net_pay": 80000,
}


class FakeDocumentRepository:
    """Provide an in-memory DocumentRepository test double."""

    def __init__(self) -> None:
        self.rows = {}

    @staticmethod
    def public(row):
        return {key: value for key, value in row.items() if key != "page_texts"}

    def upsert(self, record, public_base_url):
        previous = self.rows.get(record.name, {})
        document_id = previous.get("document_id", f"doc-{len(self.rows) + 1}")
        values = asdict(record)
        values["document_date"] = record.document_date.isoformat() if record.document_date else None
        row = {
            **values,
            "tags": list(record.tags),
            "updated_at": "test",
            "hidden": previous.get("hidden", False),
            "favorite": previous.get("favorite", False),
            "document_id": document_id,
            "public_url": f"{public_base_url}{PUBLIC_DOCUMENT_PATH}{document_id}" if public_base_url else None,
        }
        self.rows[record.name] = row
        return self.public(row)

    def get(self, name, hidden=False):
        row = self.rows.get(name)
        if row is None or (hidden is not None and row["hidden"] != hidden):
            return None
        return self.public(row)

    def pages(self, name, hidden=False):
        row = self.rows.get(name)
        return row["page_texts"] if row and row["hidden"] == hidden else None

    def list(self, hidden=False, doc_types=None, financial_year=None):
        return [
            self.public(row)
            for row in self.rows.values()
            if (hidden is None or row["hidden"] == hidden)
            and (not doc_types or row["doc_type"] in doc_types)
            and (not financial_year or row["financial_year"] == financial_year)
        ]

    def list_favorites(self):
        return [self.public(row) for row in self.rows.values() if row["favorite"] and not row["hidden"]]

    def is_hidden(self, name):
        if name not in self.rows:
            raise FileNotFoundError(name)
        return self.rows[name]["hidden"]

    def find_visible_by_content(self, content_sha256):
        return next(
            (
                self.public(row)
                for row in self.rows.values()
                if row["content_sha256"] == content_sha256 and not row["hidden"]
            ),
            None,
        )

    def name_for_document_id(self, document_id):
        return next(
            (name for name, row in self.rows.items() if row["document_id"] == document_id), None
        )

    def delete(self, name):
        self.rows.pop(name, None)

    def rename(self, name, new_name):
        if new_name in self.rows:
            raise FileExistsError(new_name)
        row = self.rows.get(name)
        if row is None or row["hidden"]:
            raise FileNotFoundError(name)
        self.rows[new_name] = {**self.rows.pop(name), "name": new_name}
        return self.public(self.rows[new_name])

    def update_metadata(self, name, title, summary, tags, extracted, extraction_error, review_flags, facts, hidden=False):
        row = self.rows.get(name)
        if row is None or row["hidden"] != hidden:
            raise FileNotFoundError(name)
        row.update(
            title=title,
            summary=summary,
            tags=tags,
            extracted=extracted,
            extraction_error=extraction_error,
            review_flags=review_flags,
            financial_year=facts.financial_year,
            document_date=str(facts.document_date) if facts.document_date else None,
            issuer=facts.issuer,
            primary_amount=facts.amount,
        )
        return self.public(row)

    def set_hidden(self, names, hidden):
        return self._set_flag(names, "hidden", hidden, include_hidden=True)

    def set_favorite(self, names, favorite):
        return self._set_flag(names, "favorite", favorite, include_hidden=False)

    def _set_flag(self, names, field, value, include_hidden):
        changed = []
        for name in names:
            row = self.rows.get(name)
            if row and (include_hidden or not row["hidden"]) and row[field] != value:
                row[field] = value
                changed.append(name)
        return changed

    def search(self, query, tags, match_all, doc_types, financial_year, limit, offset):
        words = query.split()
        matches = [
            self.public(row)
            for row in self.rows.values()
            if not row["hidden"]
            and (not doc_types or row["doc_type"] in doc_types)
            and (not financial_year or row["financial_year"] == financial_year)
            and (not tags or set(tags) & set(row["tags"]))
            and all(
                word.rstrip("s") in " ".join([row["title"], row["summary"], *row["tags"], *row["page_texts"]]).lower()
                for word in words
            )
        ]
        return len(matches), matches[offset : offset + limit]


class FakePublicURLRepository:
    """Provide an in-memory PublicURLRepository bound to the document rows."""

    def __init__(self, documents: FakeDocumentRepository) -> None:
        self.documents = documents
        self.base_url = ""

    def get(self):
        return self.base_url

    def seed(self, default_url):
        if not self.base_url:
            self.base_url = default_url
        if self.base_url:
            self._regenerate()
        return self.base_url

    def replace(self, public_base_url):
        self.base_url = public_base_url
        return self._regenerate()

    def _regenerate(self):
        for row in self.documents.rows.values():
            row["public_url"] = f"{self.base_url}{PUBLIC_DOCUMENT_PATH}{row['document_id']}"
        return len(self.documents.rows)


class FakeParser:
    """Read the PDF text layer instead of running Docling's models."""

    def __init__(self) -> None:
        self.reader = DocumentReader()

    def parse(self, data, file_format):
        return DocumentContent(file_format, self.reader.text_layer(data), "fake")


class FakeAnalyzer:
    """Classify as a payslip (or the hinted type) and extract fixed details, without a model."""

    model = "test-model"
    images_with_text = False

    def __init__(self) -> None:
        self.reader = DocumentReader()
        self.extracted = dict(PAYSLIP)

    def page_images(self, data):
        return []

    def classify(self, data, content, hint):
        del data, content
        return DocumentClassification(
            doc_type=hint.key if hint else "payslip",
            title="Acme Corp payslip, March 2025",
            summary="Salary slip for Asha Rao from Acme Corp.",
            tags=["acme corp", "salary"],
            confidence=0.9,
        )

    def extract(self, data, content, kind, from_images=False):
        del data, content, from_images
        return kind.schema.model_validate(self.extracted if kind.key == "payslip" else {})


class StubParser:
    """Return fixed parsed content, so pipeline tests control what the agents see."""

    def __init__(self, content: DocumentContent) -> None:
        self.content = content

    def parse(self, data, file_format):
        del data, file_format
        return self.content


class ScriptedChat:
    """An OllamaChat stand-in whose replies come from a list, recording each request."""

    model = "scripted-model"

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.requests = []

    def respond(self, messages, info):
        self.requests.append(messages)
        reply = self.replies.pop(0)
        return ModelResponse(parts=[TextPart(reply if isinstance(reply, str) else json.dumps(reply))])

    def as_model(self):
        return FunctionModel(self.respond, model_name=self.model)


def minimal_pdf(lines: list[str], pages: int = 1) -> bytes:
    """Build a small valid PDF whose pages carry ``lines`` as real text."""
    stream = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(pages))
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>"]
    font = 3 + 2 * pages
    for index in range(pages):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {4 + 2 * index} 0 R >>"
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    output, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    output += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return output


def payslip_pdf(marker: str = "") -> bytes:
    return minimal_pdf(["ACME CORP PAYSLIP MARCH 2025", "Employee Asha Rao", f"Net Pay 80,000.00 {marker}"])


def pdf_payload(marker: str = "") -> str:
    return base64.b64encode(payslip_pdf(marker)).decode("ascii")


def image_bytes(image_format="PNG", color="white", size=(40, 60)):
    """Create small valid image bytes in ``image_format``."""
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format=image_format)
    return buffer.getvalue()


def payslip_details(**changes) -> Payslip:
    return Payslip.model_validate({**PAYSLIP, **changes})


def docx_bytes(text: str = "Offer letter for Asha Rao") -> bytes:
    """A minimal Word document with one paragraph."""
    body = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    return _ooxml(
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        body,
    )


def xlsx_bytes(columns: int = 30, rows: int = 40) -> bytes:
    """A minimal one-sheet workbook of numbers, wider than a printed page."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationships = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    cells = "".join(
        f'<row r="{row}">' + "".join(f"<c><v>{row * column}</v></c>" for column in range(1, columns + 1)) + "</row>"
        for row in range(1, rows + 1)
    )
    return _ooxml(
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        f'<workbook xmlns="{main}" xmlns:r="{relationships}">'
        '<sheets><sheet name="Salary" sheetId="1" r:id="rId1"/></sheets></workbook>',
        extra={
            "xl/_rels/workbook.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
                f'Type="{relationships}/worksheet"/></Relationships>'
            ),
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{main}"><sheetData>{cells}</sheetData></worksheet>',
        },
        overrides={
            "/xl/worksheets/sheet1.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
        },
    )


def _ooxml(main_part: str, main_type: str, body: str, extra=None, overrides=None) -> bytes:
    parts = {f"/{main_part}": main_type, **(overrides or {})}
    content_types = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(f'<Override PartName="{name}" ContentType="{kind}"/>' for name, kind in parts.items())
        + "</Types>"
    )
    package_rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Target="{main_part}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
        "</Relationships>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", package_rels)
        package.writestr(main_part, body)
        for name, content in (extra or {}).items():
            package.writestr(name, content)
    return buffer.getvalue()


class FakeOfficeConverter:
    """Stand in for LibreOffice: each distinct Word or Excel file becomes a distinct payslip PDF."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def to_pdf(self, data: bytes, file_format: str) -> bytes:
        self.calls.append(file_format)
        return payslip_pdf(f"{file_format} {hashlib.sha256(data).hexdigest()[:8]}")
