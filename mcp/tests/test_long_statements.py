"""Tests for compact Docling tables, batched extraction of long statements, and prefetching."""

import unittest

from fakes import ScriptedChat, StubParser, image_bytes

from app.core.config import AppConfig
from app.core.models import DocumentContent, MetadataRules
from app.core.schemas import CapitalGainsStatement
from app.integrations.extraction import DocumentAnalyzer
from app.integrations.parsing import DoclingParser
from app.services.documents import DocumentReader
from app.services.pipeline import DocumentPipeline

HEADER = "| Scheme | Units | Purchase date | Sale date | Gain |"
TRADES = 60
TABLE = "\n".join(
    [
        "CAPITAL GAINS STATEMENT FY 2023-24",
        HEADER,
        "|---|---|---|---|---|",
        *(f"| Fund {n} | {n} | 2023-01-{n % 28 + 1:02d} | 2023-12-01 | {n}.5 |" for n in range(1, TRADES + 1)),
        "| Total | | | | 1860.0 |",
        "",
        "Total gain 1860.0",
    ]
)
CLASSIFICATION = {
    "doc_type": "capital_gains_statement",
    "title": "Fund capital gains, FY 2023-24",
    "summary": "Capital gains statement.",
    "tags": ["capital gains"],
    "confidence": 0.95,
}
SUMMARY = {"provider_name": "Fund House", "financial_year": "2023-24", "total_gain": 1860.0}


def rows(first: int, last: int) -> dict:
    return {"entries": [{"security_name": f"Fund {n}", "units": n, "gain": n + 0.5} for n in range(first, last + 1)]}


class CompactTableTests(unittest.TestCase):
    def test_padding_and_merged_cells_are_removed(self):
        markdown = "\n".join(
            [
                "| FORM NO. 16          | FORM NO. 16          | FORM NO. 16          |",
                "|----------------------|----------------------|----------------------|",
                "| Salary               | 1,00,000             | 1,00,000             |",
                "| Name                 | Asha                 | Rao                  |",
            ]
        )
        self.assertEqual(
            DoclingParser.compact_tables(markdown).splitlines(),
            ["| FORM NO. 16 |", "|---|---|---|", "| Salary | 1,00,000 | 1,00,000 |", "| Name | Asha | Rao |"],
        )


class BatchedExtractionTests(unittest.TestCase):
    def run_pipeline(self, replies, text=TABLE):
        self.chat = ScriptedChat(replies)
        analyzer = DocumentAnalyzer(self.chat, DocumentReader(), 24000, 3)
        content = DocumentContent("PDF", [text], "test")
        pipeline = DocumentPipeline(StubParser(content), analyzer, MetadataRules(32, frozenset()))
        return pipeline.run(image_bytes(), "capital_gains_statement")[0]

    def test_long_tables_are_read_in_batches_and_merged(self):
        analysis = self.run_pipeline([CLASSIFICATION, SUMMARY, rows(1, 25), rows(26, 50), rows(51, 60)])
        self.assertEqual(len(analysis.extracted["entries"]), TRADES)
        self.assertEqual(analysis.extracted["provider_name"], "Fund House")
        self.assertEqual(analysis.review_flags, [])
        summary_prompt, first_batch = str(self.chat.requests[1]), str(self.chat.requests[2])
        self.assertIn("[60 table rows read separately]", summary_prompt)
        self.assertNotIn("Fund 7 ", summary_prompt)
        self.assertIn("Total gain 1860.0", summary_prompt)
        self.assertIn("Rows 1-25 of 60", first_batch)
        self.assertIn(HEADER, first_batch)
        self.assertIn("Rows 51-60 of 60", str(self.chat.requests[4]))

    def test_rows_that_do_not_add_up_to_the_total_are_flagged(self):
        analysis = self.run_pipeline([CLASSIFICATION, SUMMARY, rows(1, 25), rows(26, 50), {"entries": []}])
        self.assertIn(
            "The gains of the 50 transactions add up to 1,300.00, not the total 1,860.00; "
            "some rows may be missing or misread.",
            analysis.review_flags,
        )

    def test_short_tables_still_use_one_reply(self):
        short = "\n".join(TABLE.splitlines()[: 3 + AppConfig.EXTRACTION_ROWS_PER_CALL])
        analysis = self.run_pipeline([CLASSIFICATION, {**SUMMARY, **rows(1, 2)}], text=short)
        self.assertEqual(len(self.chat.requests), 2)
        self.assertEqual(len(analysis.extracted["entries"]), 2)

    def test_the_gains_rule_compares_rows_with_the_total(self):
        details = CapitalGainsStatement.model_validate({"total_gain": 100.0, **rows(1, 3)})
        self.assertIn("The gains of the 3 transactions add up to 7.50, not the total 100.00", details.review_flags()[0])
        split = CapitalGainsStatement.model_validate({"short_term_gain": 5.0, "long_term_gain": 2.5, **rows(1, 3)})
        self.assertEqual(split.review_flags(), ["Total gain was not found."])
        short = CapitalGainsStatement.model_validate({"short_term_gain": 9.0, "long_term_gain": 2.5, **rows(1, 3)})
        self.assertIn("add up to 7.50, not the total 11.50", short.review_flags()[1])

    def test_amounts_not_printed_in_the_row_are_cleared(self):
        row = "| SBI Small Cap | 7.821 | 127.8603 | 131.7865 | 30.71 | 0 |"
        reply = {"security_name": "SBI Small Cap", "units": 7.821, "gain": 3.9262, "sale_value": 0.0}
        self.assertEqual(
            DocumentAnalyzer.printed_amounts(reply, row),
            {"security_name": "SBI Small Cap", "units": 7.821, "gain": None, "sale_value": 0.0},
        )


class CountingParser(StubParser):
    def __init__(self, content):
        super().__init__(content)
        self.calls = 0

    def parse(self, data, file_format):
        self.calls += 1
        return super().parse(data, file_format)


class PrefetchTests(unittest.TestCase):
    def test_a_prefetched_document_is_parsed_once_and_ahead_of_its_run(self):
        parser = CountingParser(DocumentContent("PDF", ["ACME CORP PAYSLIP MARCH 2025 Net Pay 80,000.00 Employee"], "test"))
        chat = ScriptedChat([CLASSIFICATION | {"doc_type": "payslip"}, {"net_pay": 80000}])
        pipeline = DocumentPipeline(parser, DocumentAnalyzer(chat, DocumentReader(), 24000, 3), MetadataRules(32, frozenset()))
        data = image_bytes()
        pipeline.prefetch(data)
        pipeline.prefetch(data)
        analysis, _ = pipeline.run(data, "payslip")
        self.assertEqual(parser.calls, 1)
        self.assertEqual(analysis.extracted["net_pay"], 80000.0)


if __name__ == "__main__":
    unittest.main()
