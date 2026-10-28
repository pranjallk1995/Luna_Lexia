"""Tests for the Luna Lexia document use cases."""

import base64
import tempfile
import textwrap
import unittest
from pathlib import Path

from fakes import (
    FakeAnalyzer,
    FakeDocumentRepository,
    FakeParser,
    FakePublicURLRepository,
    image_bytes,
    minimal_pdf,
    payslip_pdf,
    pdf_payload,
)

from app.core.config import AppConfig
from app.core.models import MetadataChange
from app.services.application import LexiaService
from app.services.uploads import UploadLinkNotFound


# An AES-256 protected copy of payslip_pdf("locked"), made with pypdf.
LOCKED_PDF = (Path(__file__).parent / "fixtures" / "locked_payslip.pdf").read_bytes()
LOCKED_PASSWORD = "ABCDE1234F"


class LexiaServiceTestCase(unittest.TestCase):
    """Build a real service graph over in-memory repositories."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.downloads_temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = FakeDocumentRepository()
        self.config = AppConfig(
            document_dir=self.root,
            database_url="postgresql://test",
            ollama_url="http://ollama:11434",
            document_model="test-model",
            public_base_url="https://lexia.example.test/vault",
            download_dir=Path(self.downloads_temp.name),
        )
        self.analyzer = FakeAnalyzer()
        self.service = LexiaService.create(
            self.config,
            self.repository,
            FakePublicURLRepository(self.repository),
            FakeParser(),
            self.analyzer,
        )
        self.visible = self.service.visible
        self.hidden = self.service.hidden

    def tearDown(self):
        self.temp.cleanup()
        self.downloads_temp.cleanup()


class IngestTests(LexiaServiceTestCase):
    def test_ingest_persists_analysis_facts_and_system_tags(self):
        result = self.visible.ingest("march.pdf", pdf_payload())
        self.assertEqual(result["doc_type"], "payslip")
        self.assertEqual(result["extracted"]["net_pay"], 80000.0)
        self.assertEqual(result["financial_year"], "2024-25")
        self.assertEqual(result["issuer"], "Acme Corp")
        self.assertEqual(result["primary_amount"], 80000.0)
        self.assertEqual(result["tags"], ["acme corp", "salary", "payslip", "fy 2024-25"])
        self.assertEqual((result["page_count"], result["file_format"]), (1, "PDF"))
        self.assertEqual(result["review_flags"], [])
        self.assertTrue((self.root / "march.pdf").is_file())
        self.assertEqual(result["public_url"], "https://lexia.example.test/vault/api/public/documents/doc-1")
        self.assertIn("Net Pay 80,000.00", self.repository.rows["march.pdf"]["page_texts"][0])

    def test_rule_failures_become_review_flags_and_tag(self):
        self.analyzer.extracted["net_pay"] = 90000
        result = self.visible.ingest("march.pdf", pdf_payload())
        [flag] = result["review_flags"]
        self.assertIn("does not equal net pay", flag)
        self.assertIn("needs review", result["tags"])

    def test_doc_type_hint_is_used(self):
        result = self.visible.ingest("form.pdf", pdf_payload(), doc_type="form_16")
        self.assertEqual(result["doc_type"], "form_16")
        self.assertIn("form 16", result["tags"])
        with self.assertRaisesRegex(ValueError, "Unknown doc_type"):
            self.visible.ingest("other.pdf", pdf_payload("b"), doc_type="receipt")

    def test_ingest_rejects_existing_name_without_replace(self):
        self.visible.ingest("march.pdf", pdf_payload())
        with self.assertRaises(FileExistsError):
            self.visible.ingest("march.pdf", pdf_payload("again"))

    def test_ingest_rejects_mismatched_format(self):
        with self.assertRaisesRegex(ValueError, "filename expects JPEG"):
            self.visible.ingest("march.jpg", pdf_payload())
        self.assertFalse((self.root / "march.jpg").exists())

    def test_ingest_rolls_file_back_when_metadata_persistence_fails(self):
        self.repository.upsert = lambda record, base: (_ for _ in ()).throw(
            RuntimeError("database unavailable")
        )
        with self.assertRaises(RuntimeError):
            self.visible.ingest("march.pdf", pdf_payload())
        self.assertFalse((self.root / "march.pdf").exists())

    def test_images_are_one_page_documents(self):
        result = self.visible.upload("scan.png", image_bytes())
        self.assertEqual((result["file_format"], result["page_count"]), ("PNG", 1))

    def test_reanalysis_selects_unindexed_changed_and_outdated_files(self):
        self.visible.ingest("march.pdf", pdf_payload())
        self.visible.ingest("may.pdf", pdf_payload("may"))
        (self.root / "april.pdf").write_bytes(payslip_pdf("april"))
        self.repository.rows["may.pdf"]["schema_version"] = 0
        preview = self.service.reanalysis.preview()
        self.assertEqual(
            [(item["name"], item["reason"]) for item in preview["documents"]],
            [("april.pdf", "not indexed"), ("may.pdf", "schema version 0")],
        )
        self.assertEqual(self.service.reanalysis.preview(include_current=True)["count"], 3)
        self.assertEqual(self.service.reanalysis.preview(names=["march.pdf"])["count"], 0)

    def test_reanalysis_runs_in_the_background_and_reports_failures(self):
        self.visible.ingest("march.pdf", pdf_payload())
        self.repository.rows["march.pdf"]["analysis_model"] = "old-model"
        (self.root / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
        reanalysis = self.service.reanalysis
        self.assertEqual(reanalysis.start()["total"], 2)
        reanalysis._worker.join(10)
        status = reanalysis.status()
        self.assertEqual((status["state"], status["done"], status["remaining"]), ("finished", 2, 0))
        self.assertEqual(status["reanalyzed"], ["march.pdf"])
        self.assertEqual([item["name"] for item in status["failed"]], ["broken.pdf"])
        self.assertNotEqual(self.repository.rows["march.pdf"]["analysis_model"], "old-model")
        self.assertEqual(reanalysis.preview()["documents"], [{"name": "broken.pdf", "doc_type": None, "reason": "not indexed"}])

    def test_a_stopped_reanalysis_ends_before_the_next_document(self):
        self.visible.ingest("march.pdf", pdf_payload())
        self.repository.rows["march.pdf"]["schema_version"] = 0
        reanalysis = self.service.reanalysis
        reanalysis._stop.set()
        reanalysis._progress = {"state": "running", "total": 1, "done": 0, "reanalyzed": [], "failed": [], "flagged": []}
        reanalysis.run(reanalysis.preview()["documents"])
        self.assertEqual((reanalysis.status()["state"], reanalysis.status()["done"]), ("stopped", 0))


class PasswordTests(LexiaServiceTestCase):
    def test_protected_pdf_requires_password_and_is_stored_unlocked(self):
        with self.assertRaisesRegex(ValueError, "password-protected"):
            self.visible.upload("march.pdf", LOCKED_PDF)
        with self.assertRaisesRegex(ValueError, "password is incorrect"):
            self.visible.upload("march.pdf", LOCKED_PDF, password="wrong")
        result = self.visible.upload("march.pdf", LOCKED_PDF, password=LOCKED_PASSWORD)
        self.assertEqual(result["status"], "created")
        stored = (self.root / "march.pdf").read_bytes()
        self.assertIn("Net Pay", self.service.indexer.reader.text_layer(stored)[0])

    def test_password_is_ignored_for_unprotected_pdfs(self):
        result = self.visible.upload("march.pdf", payslip_pdf(), password="unused")
        self.assertEqual((self.root / "march.pdf").read_bytes(), payslip_pdf())
        self.assertEqual(result["status"], "created")


class MetadataTests(LexiaServiceTestCase):
    def setUp(self):
        super().setUp()
        self.visible.ingest("march.pdf", pdf_payload())

    def test_extracted_corrections_are_validated_and_refresh_facts(self):
        result = self.visible.update_metadata(
            "march.pdf", MetadataChange(extracted={"net_pay": "79,500", "pay_date": "05/04/2025"})
        )
        self.assertEqual(result["extracted"]["net_pay"], 79500.0)
        self.assertEqual(result["extracted"]["pay_date"], "2025-04-05")
        self.assertEqual(result["primary_amount"], 79500.0)
        self.assertEqual(result["financial_year"], "2025-26")
        self.assertIn("needs review", result["tags"])

    def test_list_fields_are_replaced_parsed_and_rechecked(self):
        result = self.visible.update_metadata(
            "march.pdf",
            MetadataChange(
                extracted={
                    "earnings": [{"label": "Basic", "amount": "60,000"}, {"label": "HRA", "amount": "40,000"}],
                    "deductions": [{"label": "PF", "amount": "21,000"}],
                }
            ),
        )
        self.assertEqual([item["amount"] for item in result["extracted"]["earnings"]], [60000.0, 40000.0])
        [flag] = result["review_flags"]
        self.assertIn("Deduction lines add up to 21,000.00", flag)

    def test_unreadable_edits_are_refused_not_blanked(self):
        with self.assertRaisesRegex(ValueError, "pay_date: 'someday' could not be read"):
            self.visible.update_metadata("march.pdf", MetadataChange(extracted={"pay_date": "someday"}))
        with self.assertRaisesRegex(ValueError, r"earnings\[2\]\.amount"):
            self.visible.update_metadata(
                "march.pdf",
                MetadataChange(extracted={"earnings": [{"label": "Basic", "amount": "1"}, {"label": "HRA", "amount": "lots"}]}),
            )
        cleared = self.visible.update_metadata("march.pdf", MetadataChange(extracted={"pay_date": None}))
        self.assertIsNone(cleared["extracted"]["pay_date"])

    def test_unknown_extracted_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "not payslip fields"):
            self.visible.update_metadata("march.pdf", MetadataChange(extracted={"bonus": 1}))

    def test_mark_reviewed_clears_flags_and_tag(self):
        self.visible.update_metadata("march.pdf", MetadataChange(extracted={"net_pay": 1}))
        result = self.visible.update_metadata("march.pdf", MetadataChange(mark_reviewed=True))
        self.assertEqual(result["review_flags"], [])
        self.assertNotIn("needs review", result["tags"])

    def test_title_summary_and_tags(self):
        result = self.visible.update_metadata(
            "march.pdf",
            MetadataChange(title="March salary", summary="Pay for March.", add_tags=["Bonus Month"], remove_tags=["salary"]),
        )
        self.assertEqual((result["title"], result["summary"]), ("March salary", "Pay for March."))
        self.assertEqual(result["tags"], ["acme corp", "payslip", "fy 2024-25", "bonus month"])

    def test_update_requires_a_change(self):
        with self.assertRaisesRegex(ValueError, "Provide a title"):
            self.visible.update_metadata("march.pdf", MetadataChange())

    def test_reextract_can_force_a_type(self):
        result = self.visible.reextract("march.pdf", "invoice")
        self.assertEqual((result["status"], result["doc_type"]), ("reextracted", "invoice"))

    def test_read_text_returns_stored_pages(self):
        result = self.visible.read_text("march.pdf")
        [page] = result["pages"]
        self.assertEqual(page["page"], 1)
        self.assertIn("ACME CORP PAYSLIP", page["text"])
        self.assertIsNone(result["next_page"])
        with self.assertRaises(ValueError):
            self.visible.read_text("march.pdf", first_page=2)

    def test_read_text_continues_after_the_character_cap(self):
        self.visible.ingest("long.pdf", base64.b64encode(minimal_pdf(["x" * 60], pages=3)).decode())
        result = self.visible.read_text("long.pdf", max_chars=70)
        self.assertEqual([page["page"] for page in result["pages"]], [1, 2])
        self.assertEqual(result["next_page"], 3)

    def test_details_include_file_properties(self):
        details = self.visible.details("march.pdf")
        self.assertEqual(details["file"]["format"], "PDF")
        self.assertTrue(details["file"]["has_text_layer"])
        self.assertEqual(details["extracted"]["employer_name"], "Acme Corp")


class FilterAndSearchTests(LexiaServiceTestCase):
    def setUp(self):
        super().setUp()
        self.visible.ingest("march.pdf", pdf_payload())
        self.visible.ingest("form.pdf", pdf_payload("form"), doc_type="form_16")

    def test_list_filters_by_type_and_year(self):
        self.assertEqual([row["name"] for row in self.visible.list(["payslip"])], ["march.pdf"])
        self.assertEqual(len(self.visible.list(financial_year="FY 2024-2025")), 1)
        with self.assertRaisesRegex(ValueError, "2024-25"):
            self.visible.list(financial_year="last year")

    def test_data_page_includes_extracted_details(self):
        page = self.visible.data_page(10, 0, 100, doc_types=["payslip"])
        [item] = page["documents"]
        self.assertEqual(item["extracted"]["net_pay"], 80000.0)
        self.assertIn("review_flags", item)

    def test_search_accepts_filters_without_query(self):
        result = self.service.search.search("", None, False, 50, 0, ["form_16"])
        self.assertEqual([row["name"] for row in result["results"]], ["form.pdf"])
        with self.assertRaisesRegex(ValueError, "Provide a meaningful query"):
            self.service.search.search("my documents", None, False, 50, 0)

    def test_stats_count_types_years_and_reviews(self):
        self.visible.update_metadata("march.pdf", MetadataChange(extracted={"net_pay": 1}))
        stats = self.visible.stats()
        self.assertEqual(stats["total_documents"], 2)
        self.assertEqual(stats["by_doc_type"], {"payslip": 1, "form_16": 1})
        self.assertEqual(stats["by_financial_year"], {"unknown": 1, "2024-25": 1})
        # The corrected payslip, and the Form 16 whose empty details miss the employee PAN.
        self.assertEqual(stats["needs_review_count"], 2)


class RenameAndRemoveTests(LexiaServiceTestCase):
    def setUp(self):
        super().setUp()
        self.visible.ingest("march.pdf", pdf_payload())

    def test_remove_deletes_file_and_metadata(self):
        self.visible.remove("march.pdf")
        self.assertFalse((self.root / "march.pdf").exists())
        self.assertNotIn("march.pdf", self.repository.rows)

    def test_rename_preserves_file_and_metadata(self):
        self.visible.set_favorite(["march.pdf"], True)
        result = self.visible.rename("march.pdf", "payslip_2025_03.pdf")
        self.assertEqual((result["status"], result["old_name"]), ("renamed", "march.pdf"))
        self.assertTrue(result["favorite"])
        self.assertTrue((self.root / "payslip_2025_03.pdf").is_file())

    def test_rename_rules(self):
        for new_name in ("march 2025.pdf", "march-2025.pdf", "march.png", "../march.pdf"):
            with self.subTest(new_name=new_name), self.assertRaises(ValueError):
                self.visible.rename("march.pdf", new_name)
        self.assertTrue((self.root / "march.pdf").is_file())

    def test_rename_rolls_file_back_when_metadata_update_fails(self):
        self.repository.rename = lambda name, new_name: (_ for _ in ()).throw(RuntimeError("down"))
        with self.assertRaises(RuntimeError):
            self.visible.rename("march.pdf", "renamed.pdf")
        self.assertTrue((self.root / "march.pdf").is_file())

    def test_remove_restores_file_when_metadata_delete_fails(self):
        self.repository.delete = lambda name: (_ for _ in ()).throw(RuntimeError("down"))
        with self.assertRaises(RuntimeError):
            self.visible.remove("march.pdf")
        self.assertTrue((self.root / "march.pdf").is_file())


class HiddenAndFavoriteTests(LexiaServiceTestCase):
    def setUp(self):
        super().setUp()
        self.visible.ingest("march.pdf", pdf_payload())

    def test_hidden_documents_are_excluded_and_protected(self):
        self.hidden.set_hidden(["march.pdf"], True)
        self.assertEqual(self.visible.list(), [])
        self.assertEqual(len(self.hidden.list()), 1)
        for action in (self.visible.path, self.visible.remove, self.visible.read_text):
            with self.subTest(action=action.__name__), self.assertRaises(PermissionError):
                action("march.pdf")
        self.assertEqual(self.service.search.search("acme", None, False, 50, 0)["count"], 0)

    def test_hidden_metadata_and_remove(self):
        self.hidden.set_hidden(["march.pdf"], True)
        result = self.hidden.update_metadata("march.pdf", MetadataChange(add_tags=["private"]))
        self.assertIn("private", result["tags"])
        self.hidden.remove_many(["march.pdf"])
        self.assertFalse((self.root / "march.pdf").exists())

    def test_hidden_remove_rejects_visible_documents(self):
        with self.assertRaisesRegex(PermissionError, "Only hidden documents"):
            self.hidden.remove_many(["march.pdf"])

    def test_favorites_exclude_hidden_documents(self):
        self.visible.set_favorite(["march.pdf"], True)
        self.assertEqual([row["name"] for row in self.visible.favorites()], ["march.pdf"])
        self.hidden.set_hidden(["march.pdf"], True)
        self.assertEqual(self.visible.favorites(), [])


class ChatUploadTests(LexiaServiceTestCase):
    def test_tolerant_base64_variants_are_accepted(self):
        raw = payslip_pdf()
        standard = base64.b64encode(raw).decode()
        variants = {
            "wrapped": "\n".join(textwrap.wrap(standard, 76)),
            "unpadded": standard.rstrip("="),
            "urlsafe": base64.urlsafe_b64encode(raw).decode(),
            "data_url": f"data:application/pdf;base64,{standard}",
        }
        for index, (label, payload) in enumerate(variants.items()):
            with self.subTest(label):
                self.repository.rows.clear()
                result = self.visible.ingest(f"slip_{index}.pdf", payload)
                self.assertEqual((self.root / result["name"]).read_bytes(), raw)

    def test_upload_corrects_extension_and_numbers_taken_names(self):
        self.assertEqual(self.visible.upload("march.png", payslip_pdf())["name"], "march.pdf")
        self.assertEqual(self.visible.upload("march", payslip_pdf("b"))["name"], "march_2.pdf")
        self.assertEqual(self.visible.upload(None, image_bytes("JPEG"))["name"], "document.jpg")

    def test_upload_reports_exact_duplicate_without_second_copy(self):
        first = self.visible.upload("march.pdf", payslip_pdf())
        again = self.visible.upload("retry.pdf", payslip_pdf())
        self.assertEqual((again["status"], again["name"]), ("duplicate", first["name"]))
        self.assertFalse((self.root / "retry.pdf").exists())

    def test_unsupported_or_invalid_content_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not supported"):
            self.visible.upload("scan.gif", image_bytes("GIF"))
        with self.assertRaisesRegex(ValueError, "not a PDF"):
            self.visible.upload("notes.txt", b"hello")
        with self.assertRaisesRegex(ValueError, "not a valid PDF"):
            self.visible.upload("broken.pdf", b"%PDF-1.4 broken")


class UploadLinkTests(LexiaServiceTestCase):
    def token(self, name="march.pdf", replace=False):
        return self.service.uploads.create_link(name, replace)["upload_url"].rsplit("/", 1)[-1]

    def test_link_is_single_use_and_accepts_password(self):
        token = self.token("march")
        result = self.service.uploads.receive(token, LOCKED_PDF, password=LOCKED_PASSWORD)
        self.assertEqual((result["status"], result["name"]), ("created", "march.pdf"))
        with self.assertRaises(UploadLinkNotFound):
            self.service.uploads.receive(token, payslip_pdf("b"))

    def test_failed_upload_can_retry_on_same_link(self):
        token = self.token()
        with self.assertRaises(ValueError):
            self.service.uploads.receive(token, b"not a document")
        self.assertEqual(self.service.uploads.receive(token, payslip_pdf())["status"], "created")

    def test_replace_link_requires_existing_visible_document(self):
        with self.assertRaises(FileNotFoundError):
            self.token("missing.pdf", replace=True)
        self.visible.ingest("march.pdf", pdf_payload())
        result = self.service.uploads.receive(self.token(replace=True), payslip_pdf("new"))
        self.assertEqual(result["status"], "replaced")


if __name__ == "__main__":
    unittest.main()
