"""Tests for importing Word and Excel files, which Lexia stores as PDFs."""

import base64
import shutil
import unittest

from fakes import FakeOfficeConverter, docx_bytes, payslip_pdf, xlsx_bytes
from test_services import LexiaServiceTestCase

from app.integrations.drive import DriveFile
from app.integrations.office import OfficeConverter
from app.services.documents import OLE_SIGNATURE, DocumentPayloadDecoder, DocumentReader
from app.core.config import AppConfig


class OfficeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.decoder = DocumentPayloadDecoder({".pdf": "PDF"}, 10**6)

    def test_word_and_excel_files_are_recognized_by_content(self):
        self.assertEqual(self.decoder.detect_format(docx_bytes()), "DOCX")
        self.assertEqual(self.decoder.detect_format(xlsx_bytes()), "XLSX")
        legacy = OLE_SIGNATURE + b"\0" * 64
        self.assertEqual(self.decoder.detect_format(legacy + "WordDocument".encode("utf-16-le")), "DOC")
        self.assertEqual(self.decoder.detect_format(legacy + "Workbook".encode("utf-16-le")), "XLS")

    def test_other_zips_are_not_office_files(self):
        with self.assertRaisesRegex(ValueError, "not a PDF"):
            self.decoder.detect_format(b"PK\x03\x04 not really a zip")

    def test_password_protected_office_files_are_explained(self):
        protected = OLE_SIGNATURE + b"\0" * 64 + "EncryptedPackage".encode("utf-16-le")
        with self.assertRaisesRegex(ValueError, "password-protected"):
            self.decoder.detect_format(protected)

    def test_without_a_converter_office_files_are_refused(self):
        with self.assertRaisesRegex(ValueError, "not supported on this server"):
            self.decoder.convert(docx_bytes(), "DOCX")

    def test_word_and_excel_names_are_stored_as_pdf(self):
        self.assertEqual(self.decoder.stored_name("offer.docx"), "offer.pdf")
        self.assertEqual(self.decoder.stored_name("salary.XLS"), "salary.pdf")
        self.assertEqual(self.decoder.stored_name("scan.png"), "scan.png")
        self.assertEqual(self.decoder.name_for("offer.docx", "PDF"), "offer.pdf")

    def test_drive_word_and_excel_files_are_importable(self):
        for mime_type in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel",
        ):
            self.assertTrue(DriveFile("id", "file", mime_type).importable)


class OfficeUploadTests(LexiaServiceTestCase):
    def setUp(self):
        super().setUp()
        self.converter = FakeOfficeConverter()
        self.visible.decoder.converter = self.converter

    def test_upload_stores_the_converted_pdf_under_a_pdf_name(self):
        result = self.visible.upload("Offer Letter.docx", docx_bytes())
        self.assertEqual((result["status"], result["name"]), ("created", "Offer Letter.pdf"))
        self.assertEqual((self.root / "Offer Letter.pdf").read_bytes()[:5], b"%PDF-")
        self.assertEqual(self.converter.calls, ["DOCX"])

    def test_the_same_file_uploaded_again_is_a_duplicate(self):
        first = self.visible.upload("salary.xlsx", xlsx_bytes())
        again = self.visible.upload("salary copy.xlsx", xlsx_bytes())
        self.assertEqual((again["status"], again["name"]), ("duplicate", first["name"]))

    def test_named_ingest_and_replace_accept_office_files(self):
        created = self.visible.ingest("abc_offer.docx", base64.b64encode(docx_bytes()).decode())
        self.assertEqual(created["name"], "abc_offer.pdf")
        self.visible.ingest_bytes("slip.pdf", payslip_pdf("slip"))
        replaced = self.visible.ingest_bytes("slip.pdf", xlsx_bytes(), replace=True)
        self.assertEqual(replaced["name"], "slip.pdf")
        self.assertEqual(self.converter.calls, ["DOCX", "XLSX"])


@unittest.skipUnless(shutil.which(AppConfig.OFFICE_BINARY), "LibreOffice is not installed")
class LibreOfficeConversionTests(unittest.TestCase):
    def setUp(self):
        self.converter = OfficeConverter()
        self.reader = DocumentReader()

    def test_a_word_document_converts_with_its_text(self):
        pdf = self.converter.to_pdf(docx_bytes("Offer letter for Asha Rao"), "DOCX")
        self.assertIn("Asha Rao", "".join(self.reader.text_layer(pdf)))

    def test_each_sheet_is_one_page_and_conversion_is_reproducible(self):
        workbook = xlsx_bytes(columns=30)
        first, second = self.converter.to_pdf(workbook, "XLSX"), self.converter.to_pdf(workbook, "XLSX")
        self.assertEqual(self.reader.page_count(first), 1)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
