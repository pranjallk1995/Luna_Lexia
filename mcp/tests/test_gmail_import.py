"""Tests for Gmail imports: finding attachments, and opening locked ones in bulk."""

import base64
from unittest import mock

from cryptography.fernet import Fernet
from fakes import image_bytes, payslip_pdf
from test_imports import FakeImportRepository, ImportTestCase
from test_services import LOCKED_PASSWORD, LOCKED_PDF

from app.integrations.extraction import PasswordFinder
from app.integrations.gmail import (
    GmailAttachment,
    GmailClient,
    GmailDownload,
    GmailOAuth,
    GmailSignInExpired,
    attachments_of,
    text_of,
)
from app.services.imports import FileImporter, ImportJobs, ImportService


def encoded(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


class FakeGmail:
    def __init__(self, files):
        self.files = files
        self.downloads = 0
        self.expired = False

    def account_email(self):
        if self.expired:
            raise GmailSignInExpired()
        return "me@example.com"

    def search(self, query):
        yield from (download.attachment for download in self.files)

    def download(self, source_id):
        self.downloads += 1
        return next(download for download in self.files if download.attachment.source_id == source_id)


class FakeFinder:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls = []

    def candidates(self, subject, text, details):
        self.calls.append(details)
        return self.answers


def mail(message_id, data, text="", name="Statement.pdf", date="2026-03-12"):
    return GmailDownload(GmailAttachment(message_id, "1", name, "Your statement", "bank@example.com", date), data, text)


class GmailTestCase(ImportTestCase):
    def setUp(self):
        super().setUp()
        self.finder = FakeFinder()
        self.gmail_mailbox = FakeGmail([])
        from app.integrations.remote import DocumentSourceResolver

        sources = DocumentSourceResolver(self.service.visible.decoder, 10_000_000, 3)
        files = FileImporter(self.service.visible, sources, self.imports_repo, self.finder)
        self.imports = ImportService(
            self.imports_repo,
            self.imports.oauth,
            ImportJobs(self.imports_repo, files, run_inline=True),
            Fernet.generate_key().decode(),
            3,
            gmail_oauth=GmailOAuth("client", "secret", "https://lexia.example.test/api/gmail/callback", "x" * 32),
            gmail_factory=lambda token: self.gmail_mailbox,
        )
        self.imports.imports.save_gmail_connection("me@example.com", self.imports.cipher.encrypt(b"refresh-token").decode())

    def outcomes(self, job):
        return [row["outcome"] for row in self.imports_repo.items(str(job["job_id"]))]


class GmailConnectionTests(GmailTestCase):
    def test_status_reports_the_account(self):
        status = self.imports.gmail_status()
        self.assertEqual((status["configured"], status["connected"], status["email"], status["sign_in_expired"]), (True, True, "me@example.com", False))

    def test_token_is_encrypted_and_disconnecting_forgets_it(self):
        self.assertNotIn("refresh-token", self.imports_repo.gmail["encrypted_refresh_token"])
        self.imports.gmail_disconnect()
        self.assertFalse(self.imports.gmail_status()["connected"])
        with self.assertRaisesRegex(ValueError, "not connected"):
            self.imports.import_gmail("statement")

    def test_an_expired_sign_in_is_reported(self):
        self.gmail_mailbox.expired = True
        self.assertTrue(self.imports.gmail_status()["sign_in_expired"])

    def test_the_sign_in_asks_only_to_read_mail_and_is_signed_separately(self):
        oauth = self.imports.gmail_oauth
        url = oauth.authorization_url()
        self.assertIn("gmail.readonly", url)
        self.assertNotIn("drive", url.split("redirect_uri")[0])
        self.assertNotIn("code_challenge", url)
        drive_state = self.imports.oauth.signer.dumps({"user": "owner"})
        with self.assertRaisesRegex(PermissionError, "Gmail"):
            oauth.user_of(drive_state)


class GmailMessageTests(ImportTestCase):
    def test_attachments_leave_out_embedded_images_and_other_files(self):
        message = {
            "id": "m1",
            "internalDate": "1773273600000",
            "payload": {
                "headers": [{"name": "Subject", "value": "March statement"}, {"name": "From", "value": "bank@example.com"}],
                "parts": [
                    {"partId": "0", "mimeType": "text/html", "filename": "", "body": {"data": encoded("<p>Hi</p>")}},
                    {"partId": "1", "mimeType": "application/octet-stream", "filename": "statement.pdf", "body": {"attachmentId": "a"}},
                    {"partId": "2", "mimeType": "image/png", "filename": "logo.png", "headers": [{"name": "Content-ID", "value": "<logo>"}], "body": {"attachmentId": "b"}},
                    {"partId": "3", "mimeType": "text/calendar", "filename": "invite.ics", "body": {"attachmentId": "c"}},
                    {"partId": "4", "mimeType": "image/jpeg", "filename": "scan.jpg", "headers": [{"name": "Content-Disposition", "value": "attachment"}], "body": {"attachmentId": "d"}},
                ],
            },
        }
        found = attachments_of(message)
        self.assertEqual([attachment.name for attachment in found], ["statement.pdf", "scan.jpg"])
        self.assertEqual(found[0].source_id, "m1:1")
        self.assertEqual(found[0].upload_name, "2026-03-12_statement.pdf")

    def test_html_mail_is_read_as_text(self):
        html = "<style>p{color:red}</style><p>The password is&nbsp;<b>ABCD1234</b></p>"
        message = {"payload": {"mimeType": "text/html", "body": {"data": encoded(html)}}}
        text = text_of(message)
        self.assertIn("ABCD1234", text)
        self.assertNotIn("color", text)

    def test_download_finds_the_part_by_its_stable_id(self):
        service = mock.MagicMock()
        messages = service.users.return_value.messages.return_value
        messages.get.return_value.execute.return_value = {
            "id": "m1",
            "payload": {"parts": [{"partId": "1", "mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "fresh"}}]},
        }
        messages.attachments.return_value.get.return_value.execute.return_value = {"data": encoded("pdf-bytes")}
        download = GmailClient(service).download("m1:1")
        self.assertEqual(download.data, b"pdf-bytes")
        self.assertEqual(messages.attachments.return_value.get.call_args.kwargs["id"], "fresh")

    def test_search_only_looks_at_mail_with_attachments(self):
        service = mock.MagicMock()
        messages = service.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {"messages": []}
        self.assertEqual(list(GmailClient(service).search("from:bank OR statement")), [])
        self.assertEqual(messages.list.call_args.kwargs["q"], "(from:bank OR statement) has:attachment")


class GmailBulkImportTests(GmailTestCase):
    def test_every_attachment_of_a_search_is_imported_and_named_by_date(self):
        self.gmail_mailbox.files = [mail("m1", payslip_pdf("one")), mail("m2", payslip_pdf("two"), date="2026-04-12")]
        job = self.imports.import_gmail("from:bank statement")
        self.assertEqual(self.outcomes(job), ["imported", "imported"])
        rows = self.imports_repo.items(str(job["job_id"]))
        self.assertEqual(rows[0]["source_id"], "m1:1")
        self.assertEqual(rows[1]["name"], "2026-04-12_Statement.pdf")

    def test_running_a_search_again_only_adds_what_is_new(self):
        self.gmail_mailbox.files = [mail("m1", payslip_pdf("one"))]
        self.imports.import_gmail("statement")
        self.gmail_mailbox.files.append(mail("m2", payslip_pdf("two")))
        self.gmail_mailbox.downloads = 0
        job = self.imports.import_gmail("statement")
        self.assertEqual(self.outcomes(job), ["skipped", "imported"])
        self.assertEqual(self.gmail_mailbox.downloads, 1)

    def test_a_search_is_required_so_a_whole_mailbox_is_never_imported(self):
        with self.assertRaisesRegex(ValueError, "which mails"):
            self.imports.import_gmail("   ")

    def test_too_many_attachments_stop_before_importing(self):
        self.gmail_mailbox.files = [mail(f"m{index}", payslip_pdf(str(index))) for index in range(4)]
        job = self.imports.import_gmail("statement")
        self.assertEqual(self.imports_repo.jobs_by_id[str(job["job_id"])]["status"], "failed")
        self.assertEqual(self.gmail_mailbox.downloads, 0)

    def test_preview_imports_nothing_and_marks_what_is_already_in_lexia(self):
        self.gmail_mailbox.files = [mail("m1", payslip_pdf("one"))]
        self.imports.import_gmail("statement")
        self.gmail_mailbox.files.append(mail("m2", payslip_pdf("two")))
        self.gmail_mailbox.downloads = 0
        preview = self.imports.preview_gmail("statement")
        self.assertEqual([item["imported"] for item in preview["attachments"]], [True, False])
        self.assertEqual(self.gmail_mailbox.downloads, 0)


class LockedAttachmentTests(GmailTestCase):
    def setUp(self):
        super().setUp()
        self.gmail_mailbox.files = [mail("m1", LOCKED_PDF, "The password is your name and birth date.")]

    def test_the_model_opens_a_locked_pdf_from_its_mail(self):
        self.finder.answers = ["wrong", LOCKED_PASSWORD]
        job = self.imports.import_gmail("statement", password_details="Name: Asha, born 07-03-1990")
        self.assertEqual(self.outcomes(job), ["imported"])
        self.assertEqual(self.finder.calls, ["Name: Asha, born 07-03-1990"])

    def test_each_candidate_is_also_tried_in_each_case(self):
        self.finder.answers = [LOCKED_PASSWORD.lower()]
        self.assertEqual(self.outcomes(self.imports.import_gmail("statement")), ["imported"])

    def test_a_password_that_worked_opens_the_rest_without_the_model(self):
        self.gmail_mailbox.files = [mail("m1", LOCKED_PDF, "hint"), mail("m2", LOCKED_PDF, "no hint here")]
        self.finder.answers = [LOCKED_PASSWORD]
        job = self.imports.import_gmail("statement")
        self.assertNotIn("needs_password", self.outcomes(job))
        self.assertEqual(len(self.finder.calls), 1)

    def test_a_given_password_means_no_model_call(self):
        job = self.imports.import_gmail("statement", password=LOCKED_PASSWORD)
        self.assertEqual(self.outcomes(job), ["imported"])
        self.assertEqual(self.finder.calls, [])

    def test_still_locked_is_reported_and_tried_again_on_the_next_run(self):
        self.finder.answers = ["wrong"]
        job = self.imports.import_gmail("statement")
        self.assertEqual(self.outcomes(job), ["needs_password"])
        self.finder.answers = [LOCKED_PASSWORD]
        self.assertEqual(self.outcomes(self.imports.import_gmail("statement", password_details="more")), ["imported"])

    def test_a_locked_file_can_be_retried_with_its_password(self):
        job = self.imports.import_gmail("statement")
        source_id = self.imports_repo.items(str(job["job_id"]))[0]["source_id"]
        retry = self.imports.upload_batch([{"gmail_attachment": source_id, "password": LOCKED_PASSWORD}])
        self.assertEqual(self.outcomes(retry), ["imported"])

    def test_details_and_passwords_are_never_stored(self):
        self.finder.answers = [LOCKED_PASSWORD]
        job = self.imports.import_gmail("statement", password="given-secret", password_details="born 07-03-1990")
        job_id = str(job["job_id"])
        stored = repr((self.imports_repo.requests, self.imports_repo.jobs_by_id, self.imports_repo.item_rows))
        for secret in ("given-secret", "07-03-1990", LOCKED_PASSWORD):
            self.assertNotIn(secret, stored)
        self.assertNotIn(job_id, self.imports.jobs.files._secrets)

    def test_a_pdf_that_is_not_locked_never_reaches_the_model(self):
        self.gmail_mailbox.files = [mail("m1", payslip_pdf("open"), "password")]
        self.assertEqual(self.outcomes(self.imports.import_gmail("statement")), ["imported"])
        self.assertEqual(self.finder.calls, [])

    def test_images_are_imported_without_unlocking(self):
        self.gmail_mailbox.files = [mail("m1", image_bytes(), name="scan.png")]
        self.assertEqual(self.outcomes(self.imports.import_gmail("statement")), ["imported"])


class PasswordFinderTests(ImportTestCase):
    def finder(self, reply=None, problem=None):
        chat = mock.Mock()
        chat.as_model.return_value = "test"
        with mock.patch("app.integrations.extraction.Agent") as agent:
            agent.return_value.run_sync.return_value.output.candidates = reply or []
            agent.return_value.run_sync.side_effect = problem
            finder = PasswordFinder(chat)
        return finder

    def test_mail_without_a_clue_is_not_sent_to_the_model(self):
        finder = self.finder(["x"])
        self.assertEqual(finder.candidates("Your statement", "Thanks for banking with us.", None), [])
        finder.agent.run_sync.assert_not_called()

    def test_the_mail_and_details_go_to_the_model_as_data(self):
        finder = self.finder(["ASHA0703"])
        self.assertEqual(finder.candidates("Statement", "Your password is ...", "Name: Asha"), ["ASHA0703"])
        prompt = finder.agent.run_sync.call_args.args[0]
        self.assertIn("Name: Asha", prompt)
        self.assertIn("<email>", prompt)

    def test_too_many_candidates_are_cut(self):
        finder = self.finder([str(index) for index in range(20)])
        self.assertEqual(len(finder.candidates("", "password", None)), 6)

    def test_a_model_failure_leaves_the_pdf_locked_without_logging_details(self):
        finder = self.finder(problem=RuntimeError("secret-detail"))
        with self.assertLogs("app.integrations.extraction", "WARNING") as logs:
            self.assertEqual(finder.candidates("", "password", "secret-detail"), [])
        self.assertNotIn("secret-detail", "".join(logs.output))
