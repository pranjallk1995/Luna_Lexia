import unittest

from cryptography.fernet import Fernet

from app.services.passwords import SavedPasswords
from fakes import FakePasswordRepository, payslip_pdf
from test_services import LOCKED_PASSWORD, LOCKED_PDF, LexiaServiceTestCase


class SavedPasswordTests(LexiaServiceTestCase):
    def setUp(self):
        super().setUp()
        self.saved_rows = FakePasswordRepository()
        self.passwords = SavedPasswords(self.saved_rows, Fernet.generate_key().decode())
        self.visible.decoder.keyring = self.passwords

    def test_a_locked_pdf_opens_with_a_saved_password_and_says_which(self):
        self.passwords.save("Old bank", "nope-1234")
        self.passwords.save("Company payslip", LOCKED_PASSWORD, "payslip")
        result = self.visible.upload("march.pdf", LOCKED_PDF)
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["opened_with_saved_password"], "Company payslip")
        worked = next(row for row in self.saved_rows.rows if row["label"] == "Company payslip")
        self.assertEqual(worked["times_worked"], 1)
        self.assertNotIn(LOCKED_PASSWORD, str(self.passwords.listing()))

    def test_the_document_types_passwords_go_first_then_the_rest_the_last_that_worked_first(self):
        self.passwords.save("Any", "a")
        self.passwords.save("Card", "c", "credit_card_statement")
        self.passwords.save("Payslip", "p", "payslip")
        self.assertEqual([row["label"] for row, _ in self.passwords.candidates("credit_card_statement")], ["Card", "Any", "Payslip"])
        self.saved_rows.worked(3)
        self.assertEqual([row["label"] for row, _ in self.passwords.candidates(None)], ["Payslip", "Any", "Card"])

    def test_none_working_says_so_and_how_to_save_one(self):
        self.passwords.save("Old bank", "nope-1234")
        with self.assertRaisesRegex(ValueError, r"None of your 1 saved password opened it.*save_document_password"):
            self.visible.upload("march.pdf", LOCKED_PDF)
        with self.assertRaisesRegex(ValueError, "password is incorrect. None of your 1 saved password"):
            self.visible.upload("march.pdf", LOCKED_PDF, password="wrong")

    def test_a_given_password_wins_and_an_unlocked_pdf_needs_none(self):
        self.passwords.save("Old bank", "nope-1234")
        result = self.visible.upload("march.pdf", LOCKED_PDF, password=LOCKED_PASSWORD)
        self.assertNotIn("opened_with_saved_password", result)
        self.assertEqual(self.saved_rows.rows[0]["times_worked"], 0)
        self.assertNotIn("opened_with_saved_password", self.visible.upload("april.pdf", payslip_pdf("april")))

    def test_saving_checks_its_input_and_forgetting_removes_it(self):
        with self.assertRaisesRegex(ValueError, "label"):
            self.passwords.save("  ", "x")
        with self.assertRaisesRegex(ValueError, "doc_type"):
            self.passwords.save("Card", "x", "not_a_type")
        saved = self.passwords.save("Card", "x", "credit_card_statement")
        self.assertNotIn("encrypted_password", saved)
        self.assertNotEqual(self.saved_rows.rows[0]["encrypted_password"], "x")
        self.passwords.forget(saved["password_id"])
        self.assertEqual(self.passwords.listing()["count"], 0)
        with self.assertRaises(FileNotFoundError):
            self.passwords.forget(saved["password_id"])

    def test_without_an_encryption_key_nothing_is_saved_or_tried(self):
        keyless = SavedPasswords(FakePasswordRepository(), "")
        with self.assertRaisesRegex(ValueError, "ENCRYPTION_KEY"):
            keyless.save("Card", "x")
        self.assertEqual(keyless.candidates(), [])


if __name__ == "__main__":
    unittest.main()
