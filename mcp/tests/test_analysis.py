"""Tests for the extraction schemas, business rules, Ollama adapter, and analysis agents."""

import unittest
from datetime import date

from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, TextPart, UserPromptPart

from fakes import PAYSLIP, ScriptedChat, StubParser, image_bytes, payslip_details

from app.core.models import DocumentClassification, DocumentContent, MetadataRules
from app.core.schemas import (
    DOCUMENT_TYPES,
    BankAccountProof,
    BankStatement,
    EducationalCertificate,
    ConsultationSummary,
    Form16,
    HealthReport,
    IdentityDocument,
    InsurancePolicy,
    Invoice,
    InvestmentProof,
    LegalDocument,
    LoanStatement,
    OfferLetter,
    ProvidentFundDocument,
    RentalAgreement,
    RentReceipt,
    Resume,
    financial_year_for,
    parse_date,
)
from app.integrations.extraction import DocumentAnalyzer
from app.integrations.ollama import OllamaChat, strict_format
from app.services.documents import DocumentReader
from app.services.pipeline import DocumentPipeline

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


class CareerAndPersonalTypeTests(unittest.TestCase):
    """The types added for employment, education, and personal records."""

    def test_offer_letter_components_must_add_up_to_ctc(self):
        offer = OfferLetter.model_validate(
            {
                "annual_ctc": "18,00,000",
                "components": [
                    {"label": "Basic", "annual_amount": 720000},
                    {"label": "HRA", "annual_amount": 360000},
                    {"label": "Total CTC", "annual_amount": 1800000},
                ],
                "offer_date": "01/06/2025",
                "joining_date": "01/07/2025",
            }
        )
        self.assertEqual(offer.key_facts().amount, 1800000.0)
        [flag] = offer.review_flags()
        self.assertIn("1,080,000.00", flag)

    def test_non_financial_types_have_no_financial_year(self):
        for details in (
            EducationalCertificate.model_validate({"issue_date": "2017-09-12"}),
            IdentityDocument.model_validate({"issue_date": "2021-07-19"}),
            Resume.model_validate({"resume_date": "2026-09-18"}),
        ):
            with self.subTest(type(details).__name__):
                self.assertIsNone(details.key_facts().financial_year)
        self.assertEqual(RentalAgreement.model_validate({"agreement_date": "2026-02-17"}).key_facts().financial_year, "2025-26")

    def test_resume_roles_normalize_months(self):
        resume = Resume.model_validate(
            {"experience": [{"employer": "MathCo", "start": "Jun 2025", "end": "Present"}], "skills": ["Python"]}
        )
        self.assertEqual(resume.experience[0].start, "2025-06")
        self.assertEqual(resume.review_flags(), [])

    def test_resume_dates_keep_their_printed_precision(self):
        for printed, stored in (("2025", "2025"), ("Sep 2025", "2025-09"), ("18/09/2025", "2025-09-18"), ("soon", None)):
            with self.subTest(printed):
                self.assertEqual(Resume.model_validate({"resume_date": printed}).resume_date, stored)
        self.assertIsNone(Resume.model_validate({"resume_date": "2025"}).key_facts().document_date)
        self.assertEqual(str(Resume.model_validate({"resume_date": "18/09/2025"}).key_facts().document_date), "2025-09-18")

    def test_educational_grades_are_bounded(self):
        flags = EducationalCertificate.model_validate({"percentage": 182, "cgpa": 11}).review_flags()
        self.assertEqual(len(flags), 2)

    def test_rental_agreement_needs_landlord_pan_above_limit(self):
        self.assertTrue(RentalAgreement.model_validate({"monthly_rent": 30000}).review_flags())
        self.assertFalse(RentalAgreement.model_validate({"monthly_rent": 30000, "landlord_pan": "ABCDE1234F"}).review_flags())

    def test_insurance_premium_and_expiry(self):
        policy = InsurancePolicy.model_validate(
            {"policy_kind": "Two Wheeler Package", "base_premium": 3000, "gst": 540, "total_premium": 3600, "policy_to": "2020-01-01"}
        )
        flags = policy.review_flags()
        self.assertTrue(any("does not equal the total premium" in flag for flag in flags))
        self.assertTrue(any("ended" in flag for flag in flags))

    def test_vehicle_policies_are_motor_policies(self):
        policy = InsurancePolicy.model_validate({"policy_kind": "term_life", "vehicle_registration_number": "KA 01 AB 1234"})
        self.assertEqual((policy.policy_kind, policy.vehicle_registration_number), ("motor", "KA01AB1234"))

    def test_degree_with_a_semester_is_flagged(self):
        flags = EducationalCertificate.model_validate({"certificate_kind": "degree", "term": "Semester VIII"}).review_flags()
        self.assertTrue(any("may be a marksheet" in flag for flag in flags))

    def test_loan_agreement_date_prefers_first_emi_over_period_end(self):
        loan = LoanStatement.model_validate({"repayment_from": "05/07/2025", "period_to": "05/06/2028", "emi_amount": 6407})
        self.assertEqual(loan.key_facts().financial_year, "2025-26")

    def test_loan_agreement_emis_must_cover_the_loan(self):
        loan = LoanStatement.model_validate({"emi_amount": 5000, "tenure_months": 36, "sanctioned_amount": 150000})
        self.assertEqual(loan.review_flags(), [])
        self.assertTrue(loan.model_copy(update={"tenure_months": 12}).review_flags())

    def test_bank_proof_keeps_last_digits_and_checks_ifsc(self):
        proof = BankAccountProof.model_validate({"account_number": "1234 5678 9012", "ifsc": "sbin0001234"})
        self.assertEqual((proof.account_number_last4, proof.ifsc), ("9012", "SBIN0001234"))
        self.assertTrue(BankAccountProof.model_validate({"ifsc": "SBIN1234"}).review_flags())

    def test_pf_nominee_shares_and_optional_balance(self):
        nomination = ProvidentFundDocument.model_validate(
            {"document_kind": "Form 2", "nominees": [{"name": "A", "share_percent": 50}, {"name": "B", "share_percent": 50}]}
        )
        self.assertEqual(nomination.review_flags(), [])
        passbook = ProvidentFundDocument.model_validate({"document_kind": "passbook"})
        self.assertIn("Total balance was not found.", passbook.review_flags())

    def test_legal_documents_do_not_require_an_amount(self):
        self.assertEqual(LegalDocument.model_validate({"legal_kind": "Legal Notice"}).review_flags(), [])

    def test_classification_offers_every_type(self):
        schema = DocumentClassification.model_json_schema()
        self.assertEqual(set(schema["properties"]["doc_type"]["enum"]), set(DOCUMENT_TYPES))
        for key in ("resume", "offer_letter", "rental_agreement", "insurance_policy"):
            self.assertIn(key, DOCUMENT_TYPES)


class HealthReportTests(unittest.TestCase):
    RESULT = {"panel": "Lipid Profile", "test_name": "Cholesterol - LDL", "value": "142", "unit": "mg/dl",
              "reference_range": "<= 99.9", "reference_low": "100", "reference_high": "99.9"}

    def report(self, **fields) -> HealthReport:
        return HealthReport.model_validate({
            "lab_name": "Acme Diagnostics", "collection_date": "09/03/2026", "report_date": "09-Mar-2026",
            "package_name": "Full Body Checkup Basic", "results": [self.RESULT], **fields,
        })

    def test_results_read_as_printed(self):
        report = self.report(results=[
            self.RESULT,
            {"test_name": "Glucose", "panel": "Urine Routine & Microscopy", "value_text": "Negative", "reference_range": "Negative"},
            {"test_name": "Absolute Neutrophil Count", "value": 2720, "value_text": "2.72", "reference_range": "2 - 7"},
            {"test_name": "Estradiol (E2)", "value_text": "< 24", "unit": "pg/mL", "reference_range": "11-44"},
            {"test_name": "Fasting Plasma Glucose", "value": 83, "reference_range": "99 or below"},
        ])
        ldl, glucose, neutrophils, estradiol, fasting = report.results
        # Only the printed upper limit: the model's lower limit came from a table of categories.
        self.assertEqual((ldl.value, ldl.reference_low, ldl.reference_high), (142.0, None, 99.9))
        self.assertTrue(ldl.out_of_range())
        self.assertEqual((glucose.value, glucose.value_text, glucose.panel), (None, "Negative", "Urine Routine & Microscopy"))
        self.assertEqual((neutrophils.value, neutrophils.reference_low, neutrophils.reference_high), (2.72, 2.0, 7.0))
        self.assertEqual((estradiol.value, estradiol.value_text, estradiol.panel), (None, "< 24", "Hormones"))
        self.assertEqual((fasting.reference_low, fasting.reference_high), (None, 99.0))
        self.assertEqual(report.review_flags(), [])

    def test_a_health_report_has_a_lab_and_date_but_no_financial_year(self):
        facts = self.report().key_facts()
        self.assertEqual((facts.issuer, facts.document_date, facts.financial_year), ("Acme Diagnostics", date(2026, 3, 9), None))

    def test_risk_categories_give_the_healthy_category_limits(self):
        # one lab's lipid profile: the model read the category boundaries as the interval.
        rows = [
            ("Cholesterol - Total", 221, "Low (desirable): < 200 mg/dL Moderate (borderline) 200-239 mg/dL High: >/= 240 mg/dL", 200, 240),
            ("Triglycerides", 98, "Normal: <150, Borderline: 150 - 199, High:200-499, Very High>=500", 150, 500),
            ("Cholesterol - HDL", 47, "High risk <40mg/dL Low risk>=40mg/dL", 40, 40),
            ("Cholesterol - LDL", 161, "Desirable: <100 Above desirable: 100 - 129 Borderline high : 130 - 159 "
             "High : 160 - 189 Very high : >=190", 100, 190),
            ("Cholesterol : HDL Cholesterol", 4.9, "Desirable : 3.5-4.5 High Risk : >5", 3.5, 4.5),
            ("Non HDL Cholesterol", 176, "Desirable:< 130, Above Desirable:130 - 159, Borderline High:160 - 189, "
             "High:190 - 219, Very High: >= 220", 130, 220),
            ("Lp(a)", 12, "High risk: > 50", 0, 50),
        ]
        report = self.report(results=[
            {"test_name": name, "value": value, "unit": "mg/dL", "reference_range": printed,
             "reference_low": low, "reference_high": high, "flag": "high" if name == "Cholesterol - HDL" else None}
            for name, value, printed, low, high in rows
        ])
        limits = [(result.reference_low, result.reference_high) for result in report.results]
        self.assertEqual(limits, [(None, 200), (None, 150), (40, None), (None, 100), (3.5, 4.5), (None, 130), (None, None)])
        self.assertEqual([result.out_of_range() for result in report.results], [True, False, False, True, True, True, None])
        # HDL 43 is in the low-risk band: a "high" flag the range contradicts is the model's.
        self.assertIsNone(report.results[2].flag)

    def test_tests_with_results_are_not_pending_and_order_numbers_are_not_packages(self):
        report = self.report(package_name="PO10000143292-196", pending_tests=["Cholesterol - LDL", "Vitamin D"])
        self.assertIsNone(report.package_name)
        self.assertEqual(report.pending_tests, ["Vitamin D"])
        self.assertEqual(self.report().package_name, "Full Body Checkup Basic")

    def test_dates_printed_with_a_time_and_a_month_name_are_read(self):
        report = self.report(collection_date="12/Sep/2025 08:02AM", report_date="12/Sep/2025 05:23PM")
        self.assertEqual((report.collection_date, report.report_date), (date(2025, 9, 12), date(2025, 9, 12)))
        self.assertEqual(self.report(collection_date="2025-09-12T08:02:00").collection_date, date(2025, 9, 12))
        self.assertEqual(self.report(collection_date="09-Mar-2026, 10:30 AM IST").collection_date, date(2026, 3, 9))
        # A lab value is never taken for a date.
        self.assertIsNone(parse_date("2-5 /hpf"))

    def test_a_missing_collection_date_is_read_from_its_printed_label(self):
        # Docling splits the label from its value, as on one lab's lipid profile.
        text = ("Collected Via\n\n: ACME DIAGNOSTICS PUNE\n\nLab Visit ID\n\n: LAB0000001\n\nCollection Date\n\n"
                ": 12/Sep/2025 08:02AM\n\nBarcode ID/Order ID : BC0000001 / 10000001\n\nReport Date\n\n: 12/Sep/2025 05:23PM")
        report = self.report(collection_date=None, report_date=None)
        self.assertIn(HealthReport.MISSING_COLLECTION_FLAG, report.review_flags())
        self.assertEqual(report.fill_printed_dates(text), ["collection_date", "report_date"])
        self.assertEqual((report.collection_date, report.report_date), (date(2025, 9, 12), date(2025, 9, 12)))
        self.assertEqual(report.key_facts().document_date, date(2025, 9, 12))
        self.assertNotIn(HealthReport.MISSING_COLLECTION_FLAG, report.review_flags())
        # A date the model read is kept.
        dated = self.report()
        self.assertEqual(dated.fill_printed_dates(text), [])
        self.assertEqual(dated.collection_date, date(2026, 3, 9))

    def test_section_rows_go_and_panels_get_one_name(self):
        report = self.report(results=[
            {"test_name": "Lipid Profile", "panel": "Lipid Profile", "value_text": "18 Apr 26"},
            {"test_name": "Uric Acid", "panel": "Uric Acid", "value": 5.5},
            {"test_name": "MCV", "panel": "FULL BODY CHECKUP BASIC", "value": 85.3},
            {"test_name": "Iron Serum", "panel": "Iron Studies, Basic", "value": 145},
            {"test_name": "Glycosylated Hemoglobin (HbA1c)", "value": 5.1},
            {"test_name": "Something rare", "panel": "Special Tests", "value": 1},
        ])
        self.assertEqual(
            [(result.test_name, result.panel) for result in report.results],
            [("Uric Acid", "Kidney Function"), ("MCV", "Complete Blood Count"), ("Iron Serum", "Iron Studies"),
             ("Glycosylated Hemoglobin (HbA1c)", "Diabetes Profile"),
             ("Something rare", "Special Tests")],
        )

    def test_the_summary_and_detail_pages_of_one_test_are_merged(self):
        summary = {"test_name": "Cholesterol-LDL", "value": 142, "reference_range": "<= 99.9", "previous_value": 161,
                   "previous_date": "04 Oct 2025"}
        detail = {**self.RESULT, "reference_range": "Desirable: <100", "method": "Calculated"}
        sgot = {"test_name": "Aspartate Transaminase (SGOT)", "value": 23, "unit": "U/L"}
        sgot_detail = {"test_name": "SGOT (Aspartate Aminotransferase)", "value": 23, "unit": "U/L", "method": "NADH"}
        report = self.report(results=[
            summary, detail, sgot, sgot_detail,
            {"test_name": "TSH", "value": 2.31}, {"test_name": "TSH", "value": 2.29},
            {"test_name": "Red blood cell", "value": 0, "unit": "/hpf"}, {"test_name": "Red Blood Cells", "value": "0.00", "unit": "/hpf"},
            {"test_name": "Calcium oxalate monohydrate crystals", "value": 0}, {"test_name": "Calcium oxalate dihydrate crystals", "value": 0},
        ])
        self.assertEqual(len(report.results), 7)
        ldl = report.results[0]
        self.assertEqual((ldl.previous_value, ldl.reference_range, ldl.reference_high, ldl.method), (161.0, "<= 99.9", 99.9, "Calculated"))
        self.assertEqual(report.results[1].method, "NADH")

    def test_a_flag_the_printed_range_contradicts_is_cleared(self):
        report = self.report(results=[
            {**self.RESULT, "flag": "Normal"},
            {"test_name": "LDL : HDL Cholesterol", "value": 3.19, "reference_range": "0 - 3.5", "flag": "high"},
            {"test_name": "Urea", "value": 47.2, "reference_range": "19.0 - 44.0", "flag": "H"},
            {"test_name": "Odd", "value": 1, "flag": "low"},
        ])
        self.assertEqual([result.flag for result in report.results], [None, None, "high", "low"])

    def test_units_text_and_bounds_read_cleanly(self):
        report = self.report(results=[
            {"test_name": "Cholesterol - LDL", "value": 142, "value_text": "mg/dl", "unit": "mg/dl", "reference_range": "<= 99.9"},
            {"test_name": "Estradiol (E2)", "value": 24, "value_text": "< 24", "unit": "pg/mL", "reference_range": "11-44"},
            {"test_name": "Iron Serum", "value": 145, "unit": "Âµg/dL"},
            {"test_name": "Urine Routine", "value_text": "Automated"},
            {"test_name": "Glucose", "panel": "", "value_text": "Negative", "reference_range": "Negative"},
            {"test_name": "Red blood cell", "value": 0, "unit": "/hpf"},
            {"test_name": "Thyroid Stimulating Hormone - Ultra Sensitive", "value": 2.31, "unit": "µIU/mL"},
            {"test_name": "Thyroid Stimulating Hormone -", "panel": "Hormone panel", "value": 2.31, "unit": "µIU/mL"},
        ])
        ldl, estradiol, iron, glucose, cells, tsh = report.results
        self.assertIsNone(ldl.value_text)
        self.assertEqual((estradiol.value, estradiol.value_text), (None, "< 24"))
        self.assertEqual(iron.unit, "µg/dL")
        self.assertEqual((glucose.panel, cells.panel), ("Urine Routine & Microscopy", "Urine Routine & Microscopy"))
        self.assertEqual(tsh.panel, "Thyroid Profile")

    def test_a_merged_copy_does_not_bring_back_a_contradicted_flag(self):
        summary = {"test_name": "Cholesterol - Total", "value": 212, "reference_range": "<= 199.9"}
        detail = {"test_name": "Cholesterol - Total", "value": 212, "reference_range": "Desirable: < 200", "flag": "normal"}
        self.assertIsNone(self.report(results=[summary, detail]).results[0].flag)

    def test_report_rules(self):
        flags = self.report(report_date="2026-03-01").review_flags()
        self.assertTrue(any("before the sample collection" in flag for flag in flags))
        self.assertIn("No test results were found.", HealthReport().review_flags())

    def test_lab_reports_are_not_medical_records(self):
        self.assertIn("health_report", DOCUMENT_TYPES["medical_record"].description)


class ConsultationSummaryTests(unittest.TestCase):
    VISIT = {
        "hospital": "Lakeside Hospital", "doctor": "Dr. Asha Rao", "specialty": "Cardiology",
        "consultation_date": "14/08/2026", "consultation_type": "OP, New", "diagnoses": ["hypertension"],
        "heart_rate_bpm": "76", "blood_pressure": "128/82 mmHg", "spo2_percent": "98", "height_cm": "168",
        "weight_kg": "70.2", "bmi": "24.87",
        "medications": [{"name": "AMLODIPINE-TABLET-5MG", "generic_name": "Amlodipine", "strength": "5 mg",
                         "dose_schedule": "1-0-0", "frequency": "Once daily", "timing": "after breakfast",
                         "duration": "1 month", "start_date": "Aug 14, 2026"}],
        "follow_up_tests": ["Kidney function test"], "follow_up_after": "after 1 Month",
    }

    def test_a_visit_keeps_its_doctor_vitals_medicines_and_follow_up(self):
        visit = ConsultationSummary.model_validate(self.VISIT)
        self.assertEqual((visit.systolic_bp, visit.diastolic_bp), (128.0, 82.0))
        self.assertEqual(visit.medications[0].start_date, date(2026, 8, 14))
        self.assertEqual(visit.review_flags(), [])
        facts = visit.key_facts()
        self.assertEqual((facts.issuer, facts.document_date, facts.financial_year), ("Lakeside Hospital", date(2026, 8, 14), None))

    def test_a_copied_instruction_line_and_visit_date_are_tidied(self):
        visit = ConsultationSummary.model_validate({
            **self.VISIT, "follow_up_date": "14/08/2026",
            "medications": [{"name": "X", "dose_schedule": "Twice daily ( 1 - 0 - 0 - 1 ) tablet orally after food"},
                            {"name": "Y", "dose_schedule": "1-0-1"}, {"name": "Z", "dose_schedule": "SOS"}],
        })
        self.assertIsNone(visit.follow_up_date)
        self.assertEqual([m.dose_schedule for m in visit.medications], ["1-0-0-1", "1-0-1", "SOS"])

    def test_visit_rules(self):
        flags = ConsultationSummary.model_validate({
            **self.VISIT, "blood_pressure": "70/120", "bmi": 30, "spo2_percent": 9, "follow_up_date": "2026-08-01",
        }).review_flags()
        for expected in ("lower number first", "does not match weight and height", "before the consultation", "SpO2 9%"):
            self.assertTrue(any(expected in flag for flag in flags), expected)

    def test_prescriptions_and_visit_summaries_are_consultations(self):
        self.assertIn("consultation_summary", DOCUMENT_TYPES["medical_record"].description)
        self.assertIn("prescription", DOCUMENT_TYPES["consultation_summary"].description)


class DoclingFallbackTests(unittest.TestCase):
    """Pages Docling returns empty fall back to the PDF text layer."""

    def parser(self, pages):
        from app.integrations.parsing import DoclingParser

        parser = DoclingParser(DocumentReader(), 200_000, "cpu")
        parser._docling_pages = lambda data, file_format: pages
        return parser

    def test_empty_docling_pages_use_the_text_layer(self):
        from fakes import minimal_pdf

        data = minimal_pdf(["ACME PAYSLIP", "Net Pay 80,000"], pages=2)
        content = self.parser(["## ACME PAYSLIP\nNet Pay 80,000", ""]).parse(data, "PDF")
        self.assertEqual(content.page_texts[0], "## ACME PAYSLIP\nNet Pay 80,000")
        self.assertIn("Net Pay 80,000", content.page_texts[1])
        self.assertEqual(content.parser, "docling")

    def test_docling_failure_falls_back_to_pdfium(self):
        from fakes import minimal_pdf

        parser = self.parser([])
        parser._docling_pages = lambda data, file_format: (_ for _ in ()).throw(RuntimeError("boom"))
        content = parser.parse(minimal_pdf(["ACME PAYSLIP"]), "PDF")
        self.assertEqual((content.parser, content.page_texts[0]), ("pdfium", "ACME PAYSLIP"))


class DoclingGpuSharingTests(unittest.TestCase):
    """Docling gives the GPU way to Ollama and Vibia's CLAP when memory runs short."""

    def parser(self, free_mb, results):
        from types import SimpleNamespace

        from app.integrations.parsing import DoclingParser

        parser = DoclingParser(DocumentReader(), 200_000, "auto")
        parser.devices = []

        class FakeConverter:
            def __init__(self, device):
                self.device = device

            def convert(self, stream):
                parser.devices.append(self.device)
                outcome = results[self.device]
                if isinstance(outcome, Exception):
                    raise outcome
                document = SimpleNamespace(pages={1: None}, export_to_markdown=lambda page_no: outcome)
                return SimpleNamespace(document=document, errors=[])

        parser.converter = FakeConverter
        parser._gpu_free_mb = lambda: free_mb
        parser._release_gpu = lambda: parser.devices.append("released")
        return parser

    def test_a_roomy_gpu_reads_the_document_and_releases_its_working_memory(self):
        parser = self.parser(6000, {"cuda": "Net Pay 80,000"})
        self.assertEqual(parser._docling_pages(b"%PDF", "PDF"), ["Net Pay 80,000"])
        self.assertEqual(parser.devices, ["cuda", "released"])

    def test_a_busy_gpu_sends_the_document_to_the_cpu(self):
        parser = self.parser(1500, {"cpu": "Net Pay 80,000"})
        self.assertEqual(parser._docling_pages(b"%PDF", "PDF"), ["Net Pay 80,000"])
        self.assertEqual(parser.devices, ["cpu"])

    def test_running_out_of_gpu_memory_reads_the_document_again_on_the_cpu(self):
        try:
            raise RuntimeError("CUDA out of memory. Tried to allocate 512.00 MiB")
        except RuntimeError as error:
            try:
                raise RuntimeError("Pipeline StandardPdfPipeline failed") from error
            except RuntimeError as wrapped:
                failure = wrapped
        parser = self.parser(6000, {"cuda": failure, "cpu": "Net Pay 80,000"})
        self.assertEqual(parser._docling_pages(b"%PDF", "PDF"), ["Net Pay 80,000"])
        self.assertEqual(parser.devices, ["cuda", "released", "cpu"])

    def test_other_gpu_errors_still_raise(self):
        parser = self.parser(6000, {"cuda": RuntimeError("boom")})
        with self.assertRaises(RuntimeError):
            parser._docling_pages(b"%PDF", "PDF")


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


class PipelineTests(unittest.TestCase):
    """The analysis pipeline, driven by the real agents with scripted model replies."""

    def run_pipeline(self, replies, content=CONTENT, data=None, doc_type=None, images_with_text=True):
        self.chat = ScriptedChat(replies)
        analyzer = DocumentAnalyzer(self.chat, DocumentReader(), 24000, 3, images_with_text)
        pipeline = DocumentPipeline(StubParser(content), analyzer, MetadataRules(32, frozenset()))
        analysis, _ = pipeline.run(data if data is not None else image_bytes(), doc_type)
        return analysis

    @staticmethod
    def sent_images(request) -> bool:
        return any(getattr(item, "is_image", False) for item in request[-1].parts[-1].content)

    def test_text_route_classifies_extracts_and_tags(self):
        analysis = self.run_pipeline([CLASSIFICATION, PAYSLIP])
        self.assertEqual(analysis.doc_type, "payslip")
        self.assertEqual(analysis.extracted["net_pay"], 80000.0)
        self.assertEqual(analysis.tags, ["acme corp", "salary", "payslip", "fy 2024-25"])
        self.assertEqual(analysis.review_flags, [])
        self.assertIn("ACME CORP PAYSLIP", str(self.chat.requests[0]))
        self.assertIn("ACME CORP PAYSLIP", str(self.chat.requests[1]))
        self.assertTrue(self.sent_images(self.chat.requests[1]))

    def test_page_images_can_be_left_out_of_the_text_route(self):
        self.run_pipeline([CLASSIFICATION, PAYSLIP], images_with_text=False)
        self.assertFalse(self.sent_images(self.chat.requests[0]))
        self.assertFalse(self.sent_images(self.chat.requests[1]))

    def test_a_malformed_pan_is_asked_for_again(self):
        analysis = self.run_pipeline([CLASSIFICATION, {**PAYSLIP, "pan": "PYBOM1082174/18784"}, PAYSLIP])
        self.assertEqual(analysis.extracted["pan"], "ABCDE1234F")
        self.assertEqual(analysis.review_flags, [])
        feedback = OllamaChat.messages(self.chat.requests[2], None)[-1]["content"]
        self.assertIn("'PYBOM1082174/18784' is not a valid PAN", feedback)

    def test_a_pan_still_malformed_after_retries_is_kept_and_flagged(self):
        wrong = {**PAYSLIP, "pan": "TZ163"}
        analysis = self.run_pipeline([CLASSIFICATION, wrong, wrong, wrong])
        self.assertEqual(analysis.extracted["pan"], "TZ163")
        self.assertIsNone(analysis.extraction_error)
        self.assertIn("Pan 'TZ163' is not a valid PAN.", analysis.review_flags)

    def test_schema_errors_are_retried_with_feedback(self):
        broken = {**PAYSLIP, "earnings": "lots"}
        analysis = self.run_pipeline([CLASSIFICATION, broken, PAYSLIP])
        self.assertEqual(analysis.extracted["net_pay"], 80000.0)
        self.assertEqual(len(self.chat.requests), 3)
        feedback = OllamaChat.messages(self.chat.requests[2], None)[-1]
        self.assertEqual(feedback["role"], "user")
        self.assertIn("Input should be a valid array", feedback["content"])

    def test_failed_text_extraction_falls_back_to_page_images(self):
        analysis = self.run_pipeline([CLASSIFICATION, "no", "still no", "nope", PAYSLIP])
        self.assertEqual(analysis.extracted["net_pay"], 80000.0)
        self.assertIsNone(analysis.extraction_error)
        self.assertIn(DocumentPipeline.IMAGE_FALLBACK_FLAG, analysis.review_flags)
        self.assertIn("needs review", analysis.tags)
        self.assertTrue(self.sent_images(self.chat.requests[-1]))

    def test_failure_on_both_routes_is_flagged_not_raised(self):
        analysis = self.run_pipeline([CLASSIFICATION, *["not json"] * 6])
        self.assertEqual(analysis.extracted, {})
        self.assertIn("could not be extracted", analysis.extraction_error)
        self.assertIn(DocumentPipeline.FAILED_FLAG, analysis.review_flags)

    def test_documents_without_text_go_straight_to_page_images(self):
        analysis = self.run_pipeline([CLASSIFICATION, PAYSLIP], content=DocumentContent("PNG", [""], "test"))
        self.assertIn(DocumentPipeline.NO_TEXT_FLAG, analysis.review_flags)
        self.assertEqual(len(self.chat.requests), 2)
        self.assertTrue(self.sent_images(self.chat.requests[1]))

    def test_hint_overrides_type_and_disagreement_is_flagged(self):
        analysis = self.run_pipeline([CLASSIFICATION, {}], doc_type="invoice")
        self.assertEqual(analysis.doc_type, "invoice")
        self.assertTrue(any("reads like a Payslip" in flag for flag in analysis.review_flags))

    def test_low_confidence_is_flagged(self):
        analysis = self.run_pipeline([{**CLASSIFICATION, "confidence": 0.3}, PAYSLIP])
        self.assertTrue(any("low confidence" in flag for flag in analysis.review_flags))


if __name__ == "__main__":
    unittest.main()
