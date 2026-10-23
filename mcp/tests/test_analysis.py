"""Tests for the extraction schemas, business rules, Ollama adapter, and analysis agents."""

import unittest
from datetime import date

from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, TextPart, UserPromptPart

from fakes import PAYSLIP, ScriptedChat, image_bytes, payslip_details

from app.core.models import DocumentClassification, DocumentContent, MetadataRules
from app.core.schemas import (
    DOCUMENT_TYPES,
    BankStatement,
    Form16,
    Invoice,
    InvestmentProof,
    RentReceipt,
    financial_year_for,
)
from app.integrations.extraction import DocumentAnalyzer
from app.integrations.ollama import OllamaChat, strict_format
from app.services.documents import DocumentReader

CLASSIFICATION = {
    "doc_type": "payslip",
    "title": "Acme Corp payslip, March 2025",
    "summary": "Salary slip for Asha Rao.",
    "tags": ["Acme Corp", "Salary"],
    "confidence": 0.93,
}
CONTENT = DocumentContent(
    "PDF", ["ACME CORP PAYSLIP MARCH 2025\nEmployee Asha Rao\nGross 1,00,000 Deductions 20,000\nNet Pay 80,000.00"], "test"
)


class SchemaParsingTests(unittest.TestCase):
    def test_indian_formats_are_normalized(self):
        details = payslip_details(
            net_pay="Rs. 1,23,456.50",
            pan="abcde 1234f",
            pay_period="March 2025",
            pay_date="31-Mar-2025",
            bank_account_last4="XXXX XXXX 9876",
            gross_earnings=None,
        )
        self.assertEqual(details.net_pay, 123456.5)
        self.assertEqual(details.pan, "ABCDE1234F")
        self.assertEqual(details.pay_period, "2025-03")
        self.assertEqual(details.pay_date, date(2025, 3, 31))
        self.assertEqual(details.bank_account_last4, "9876")

    def test_unreadable_values_become_null_instead_of_failing(self):
        details = payslip_details(pay_date="sometime in spring", net_pay="about eighty", employer_name="  ")
        self.assertIsNone(details.pay_date)
        self.assertIsNone(details.net_pay)
        self.assertIsNone(details.employer_name)

    def test_implausible_dates_are_dropped(self):
        self.assertIsNone(payslip_details(pay_date="3103-03-20").pay_date)
        self.assertEqual(payslip_details(pay_date="3103-03-20").key_facts().financial_year, "2024-25")

    def test_total_rows_are_not_summed_as_lines(self):
        earnings = [{"label": "Basic", "amount": 60000}, {"label": "Special", "amount": 40000}, {"label": "Gross Earnings", "amount": 100000}]
        self.assertEqual(payslip_details(earnings=earnings).review_flags(), [])

    def test_choices_match_leniently(self):
        self.assertEqual(Form16.model_validate({"tax_regime": "New Regime (115BAC)"}).tax_regime, "new")
        self.assertEqual(InvestmentProof.model_validate({"proof_kind": "Life Insurance Premium"}).proof_kind, "life_insurance")
        self.assertIsNone(InvestmentProof.model_validate({"proof_kind": "lottery"}).proof_kind)

    def test_financial_year_comes_from_year_fields_or_dates(self):
        self.assertEqual(Form16.model_validate({"assessment_year": "AY 2025-2026"}).key_facts().financial_year, "2024-25")
        self.assertEqual(payslip_details(pay_date="2025-04-01").key_facts().financial_year, "2025-26")
        self.assertEqual(financial_year_for(date(2025, 3, 31)), "2024-25")

    def test_every_type_has_a_json_schema(self):
        for key, kind in DOCUMENT_TYPES.items():
            with self.subTest(key):
                self.assertIn("properties", kind.describe()["fields"])


class BusinessRuleTests(unittest.TestCase):
    def test_consistent_payslip_has_no_flags(self):
        self.assertEqual(payslip_details().review_flags(), [])

    def test_payslip_arithmetic_and_identifiers(self):
        flags = payslip_details(net_pay=85000, pan="ABC123").review_flags()
        self.assertTrue(any("does not equal net pay" in flag for flag in flags))
        self.assertTrue(any("not a valid PAN" in flag for flag in flags))
        self.assertEqual(payslip_details(pan="XXXXX1234X").review_flags(), [])

    def test_missing_key_amount_is_flagged(self):
        self.assertIn("Net pay was not found.", payslip_details(net_pay=None, gross_earnings=None).review_flags())

    def test_form16_rules(self):
        flags = Form16.model_validate(
            {
                "assessment_year": "2025-26",
                "financial_year": "2023-24",
                "quarterly_tds": [{"tax_deducted": 1000}, {"tax_deducted": 2000}],
                "total_tax_deducted": 5000,
            }
        ).review_flags()
        self.assertTrue(any("Employee PAN" in flag for flag in flags))
        self.assertTrue(any("Quarterly TDS adds up to 3,000.00" in flag for flag in flags))
        self.assertTrue(any("does not follow financial year" in flag for flag in flags))

    def test_bank_balance_and_period_rules(self):
        statement = BankStatement.model_validate(
            {
                "statement_from": "2025-03-01",
                "statement_to": "2025-03-31",
                "opening_balance": 1000,
                "total_credits": 500,
                "total_debits": 200,
                "closing_balance": 1300,
                "transactions": [{"date": "2025-04-02", "debit": 10}],
            }
        )
        self.assertEqual(statement.review_flags(), ["1 transaction(s) fall outside the statement period."])

    def test_bank_running_balance_catches_swapped_columns(self):
        statement = BankStatement.model_validate(
            {
                "opening_balance": 1000,
                "closing_balance": 1500,
                "transactions": [
                    {"debit": 100, "balance": 900},
                    {"debit": 600, "balance": 1500},
                ],
            }
        )
        self.assertTrue(any("running balance" in flag for flag in statement.review_flags()))

    def test_form16_salary_head_catches_misread_professional_tax(self):
        form = Form16.model_validate(
            {
                "employee_pan": "BQRPK4521M",
                "gross_salary": 1440000,
                "exemptions_section_10": 120000,
                "standard_deduction": 50000,
                "professional_tax": 240,
                "income_chargeable_under_salaries": 1267600,
                "tax_on_total_income": 140280,
                "health_education_cess": 5611,
                "net_tax_payable": 145891,
                "total_tax_deducted": 163500,
            }
        )
        [flag] = form.review_flags()
        self.assertIn("income under salaries", flag)
        self.assertEqual(form.model_copy(update={"professional_tax": 2400}).review_flags(), [])

    def test_last_four_digit_fields_take_the_full_number(self):
        statement = BankStatement.model_validate({"account_number": "00000032145698765"})
        self.assertEqual(statement.account_number_last4, "8765")
        self.assertIn("account_number", BankStatement.model_json_schema()["properties"])
        self.assertEqual(statement.model_dump()["account_number_last4"], "8765")
        self.assertEqual(BankStatement.model_validate(statement.model_dump()).account_number_last4, "8765")

    def test_invoice_rules(self):
        invoice = Invoice.model_validate(
            {"subtotal": 1000, "cgst": 90, "sgst": 90, "total_tax": 180, "total_amount": 1200}
        )
        self.assertTrue(any("does not equal the total" in flag for flag in invoice.review_flags()))

    def test_rent_above_limit_needs_landlord_pan(self):
        self.assertTrue(RentReceipt.model_validate({"monthly_rent": 25000, "amount_received": 25000}).review_flags())
        self.assertFalse(RentReceipt.model_validate({"monthly_rent": 5000, "amount_received": 5000}).review_flags())

    def test_reversed_periods_are_flagged(self):
        flags = BankStatement.model_validate(
            {"statement_from": "2025-04-01", "statement_to": "2025-03-01", "closing_balance": 1}
        ).review_flags()
        self.assertTrue(any("is after" in flag for flag in flags))


class OllamaAdapterTests(unittest.TestCase):
    def test_strict_format_requires_every_field_and_drops_noise(self):
        schema = strict_format(DOCUMENT_TYPES["payslip"].schema.model_json_schema())
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertNotIn("title", schema["properties"]["net_pay"])
        self.assertNotIn("format", str(schema["properties"]["pay_date"]))
        line_item = schema["$defs"]["LineItem"]
        self.assertEqual(line_item["required"], ["label", "amount"])

    def test_strict_format_keeps_fields_named_like_annotations(self):
        schema = strict_format(DocumentClassification.model_json_schema())
        self.assertIn("title", schema["properties"])
        self.assertIn("title", schema["required"])

    def test_messages_include_instructions_replies_and_retries(self):
        retry = RetryPromptPart(content="net_pay: Input should be a valid number")
        history = [
            ModelRequest(parts=[UserPromptPart(content="Extract this.")]),
            ModelResponse(parts=[TextPart(content='{"net_pay": "x"}')]),
            ModelRequest(parts=[retry]),
        ]
        chat = OllamaChat.messages(history, "Be exact.")
        self.assertEqual([message["role"] for message in chat], ["system", "user", "assistant", "user"])
        self.assertIn("valid number", chat[-1]["content"])


class AnalyzerTests(unittest.TestCase):
    def analyzer(self, replies):
        self.chat = ScriptedChat(replies)
        return DocumentAnalyzer(self.chat, MetadataRules(32, frozenset()), DocumentReader(), 24000, 3)

    def test_classifies_then_extracts_validated_details(self):
        analysis = self.analyzer([CLASSIFICATION, PAYSLIP]).analyze(b"", CONTENT)
        self.assertEqual(analysis.doc_type, "payslip")
        self.assertEqual(analysis.tags, ["acme corp", "salary"])
        self.assertEqual(analysis.extracted["net_pay"], 80000.0)
        self.assertEqual(analysis.facts.financial_year, "2024-25")
        self.assertEqual(analysis.review_flags, [])
        self.assertIn("ACME CORP PAYSLIP", str(self.chat.requests[0]))

    def test_schema_errors_are_retried_with_feedback(self):
        broken = {**PAYSLIP, "earnings": "lots"}
        analysis = self.analyzer([CLASSIFICATION, broken, PAYSLIP]).analyze(b"", CONTENT)
        self.assertEqual(analysis.extracted["net_pay"], 80000.0)
        self.assertEqual(len(self.chat.requests), 3)
        feedback = OllamaChat.messages(self.chat.requests[2], None)[-1]
        self.assertEqual(feedback["role"], "user")
        self.assertIn("Input should be a valid array", feedback["content"])

    def test_extraction_failure_is_flagged_not_raised(self):
        replies = [CLASSIFICATION, "not json", "still not json", "nope"]
        analysis = self.analyzer(replies).analyze(b"", CONTENT)
        self.assertEqual(analysis.extracted, {})
        self.assertIsNotNone(analysis.extraction_error)
        self.assertTrue(any("could not be extracted" in flag for flag in analysis.review_flags))

    def test_hint_overrides_type_and_disagreement_is_flagged(self):
        analysis = self.analyzer([CLASSIFICATION, {}]).analyze(b"", CONTENT, "invoice")
        self.assertEqual(analysis.doc_type, "invoice")
        self.assertTrue(any("reads like a Payslip" in flag for flag in analysis.review_flags))

    def test_documents_without_text_are_sent_as_page_images(self):
        content = DocumentContent("PNG", [""], "test")
        analysis = self.analyzer([CLASSIFICATION, PAYSLIP]).analyze(image_bytes(), content)
        self.assertIn("No text could be read", " ".join(analysis.review_flags))
        [request] = self.chat.requests[0]
        self.assertTrue(any(getattr(item, "is_image", False) for item in request.parts[-1].content))

    def test_low_confidence_is_flagged(self):
        analysis = self.analyzer([{**CLASSIFICATION, "confidence": 0.3}, PAYSLIP]).analyze(b"", CONTENT)
        self.assertTrue(any("low confidence" in flag for flag in analysis.review_flags))


if __name__ == "__main__":
    unittest.main()
