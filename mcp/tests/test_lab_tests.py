import unittest

import fakes  # noqa: F401 - puts the app on the import path

from app.services.lab_tests import ABOUT_MAX_CHARS, BATCH, LabTestService


class FakeLabTestRepository:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def all(self):
        return sorted(self.rows.values(), key=lambda row: row["test_name"])

    def known(self, keys):
        return {key for key in keys if key in self.rows}

    def save(self, rows, source):
        saved = 0
        for key, name, about in rows:
            if source == "model" and key in self.rows:
                continue
            self.rows[key] = {"test_key": key, "test_name": name, "about": about, "source": source}
            saved += 1
        return saved


class FakeDocuments:
    def __init__(self, reports) -> None:
        self.reports = reports

    def list(self, hidden=False, doc_types=None, financial_year=None):
        return [report for report in self.reports if not doc_types or report["doc_type"] in doc_types]


class ScriptedExplainer:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def explain(self, names):
        self.calls.append(list(names))
        # The model skips one name, so it stays unexplained.
        return {name: f"  What {name}\n measures. " for name in names if name != "Skipped"}


def report(name, *tests):
    return {"name": name, "doc_type": "health_report",
            "extracted": {"results": [{"test_name": test, "panel": "Lipid Profile"} for test in tests]}}


class LabTestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeLabTestRepository()
        self.explainer = ScriptedExplainer()
        self.documents = FakeDocuments([
            report("apr.pdf", "Cholesterol - LDL", "HbA1c"),
            report("oct.pdf", "Cholesterol - LDL", "Cholesterol- VLDL"),
            {"name": "payslip.pdf", "doc_type": "payslip", "extracted": {}},
        ])
        self.service = LabTestService(self.repository, self.documents, self.explainer)

    def test_the_model_explains_only_tests_without_an_explanation(self):
        self.repository.save([("hba1c", "HbA1c", "Written earlier.")], "assistant")
        saved = self.service.explain_new([{"test_name": "HbA1c"}, {"test_name": "Cholesterol - LDL"},
                                          {"test_name": "cholesterol ldl"}, {"test_name": None}, {"test_name": "Skipped"}])
        self.assertEqual(saved, 1)
        # One call, with each new test once (LDL spelled two ways is one test).
        self.assertEqual(self.explainer.calls, [["Cholesterol - LDL", "Skipped"]])
        self.assertEqual(self.repository.rows["cholesterolldl"]["about"], "What Cholesterol - LDL measures.")
        self.assertEqual(self.repository.rows["hba1c"]["about"], "Written earlier.")
        self.assertNotIn("skipped", self.repository.rows)

    def test_a_long_report_is_explained_in_batches(self):
        self.service.explain_new([{"test_name": f"Test {number}"} for number in range(BATCH * 2 + 1)])
        self.assertEqual([len(call) for call in self.explainer.calls], [BATCH, BATCH, 1])

    def test_an_assistant_replaces_the_models_explanation_but_not_the_reverse(self):
        self.service.explain_new([{"test_name": "HbA1c"}])
        reply = self.service.save([{"test_name": "HbA1c", "about": "Average blood sugar over the last three months."}])
        self.assertEqual(reply, {"status": "explanations_saved", "saved": 1})
        self.service.explain_new([{"test_name": "HbA1c"}])
        self.assertEqual(self.repository.rows["hba1c"]["source"], "assistant")
        self.assertEqual(self.repository.rows["hba1c"]["about"], "Average blood sugar over the last three months.")

    def test_bad_explanations_are_refused(self):
        for bad in ([], [{"test_name": "HbA1c"}], [{"about": "Something."}], [{"test_name": "HbA1c", "about": "x" * (ABOUT_MAX_CHARS + 1)}]):
            with self.assertRaises(ValueError):
                self.service.save(bad)
        self.assertEqual(self.repository.rows, {})

    def test_listing_covers_every_stored_test_with_its_explanation(self):
        self.service.save([{"test_name": "Cholesterol - LDL", "about": "The cholesterol that builds up in arteries."}])
        listing = self.service.listing()
        self.assertEqual(listing["count"], 3)
        self.assertEqual(listing["missing"], 2)
        ldl = next(test for test in listing["tests"] if test["test_name"] == "Cholesterol - LDL")
        self.assertEqual((ldl["report_count"], ldl["source"]), (2, "assistant"))
        self.assertEqual([test["test_name"] for test in self.service.listing(missing_only=True)["tests"]],
                         ["Cholesterol- VLDL", "HbA1c"])

    def test_luna_vita_reads_explanations_by_test_key(self):
        self.service.save([{"test_name": "HbA1c", "about": "Average blood sugar."}])
        self.assertEqual(self.service.explanations()["tests"],
                         {"hba1c": {"test_name": "HbA1c", "about": "Average blood sugar.", "source": "assistant"}})

    def test_without_a_model_nothing_is_explained(self):
        service = LabTestService(self.repository, self.documents)
        self.assertEqual(service.explain_new([{"test_name": "HbA1c"}]), 0)
        service.explain_in_background([{"test_name": "HbA1c"}])
        self.assertEqual(self.repository.rows, {})


class IndexerHookTests(unittest.TestCase):
    def test_storing_a_health_report_explains_its_tests_and_other_types_do_not(self):
        from app.core.models import DocumentAnalysis, DocumentContent
        from app.core.schemas import KeyFacts
        from app.services.application import DocumentIndexer

        class Recorder:
            def __init__(self) -> None:
                self.seen = []

            def explain_in_background(self, results):
                self.seen.append(results)

        class Documents:
            def upsert(self, record, base):
                return {"name": record.name}

        class Urls:
            class repository:
                @staticmethod
                def get():
                    return ""

        class Analyzer:
            model, reader = "model", None

        class Pipeline:
            analyzer = Analyzer()

        indexer = DocumentIndexer(None, Documents(), Pipeline(), Urls())
        indexer.lab_tests = Recorder()
        content = DocumentContent(file_format="pdf", page_texts=["text"], parser="docling")
        for doc_type, extracted in (("health_report", {"results": [{"test_name": "HbA1c"}]}), ("payslip", {})):
            analysis = DocumentAnalysis(doc_type=doc_type, title="t", summary="s", tags=(), confidence=1.0,
                                        extracted=extracted, facts=KeyFacts(), review_flags=[], extraction_error=None)
            indexer.store(f"{doc_type}.pdf", b"data", analysis, content)
        self.assertEqual(indexer.lab_tests.seen, [[{"test_name": "HbA1c"}]])


class ExplainerTests(unittest.TestCase):
    def test_only_the_names_asked_about_are_kept(self):
        from fakes import ScriptedChat

        from app.integrations.extraction import LabTestExplainer

        chat = ScriptedChat([{"explanations": [
            {"test_name": "HbA1c", "about": " Average blood sugar over three months. "},
            {"test_name": "LDL Cholesterol", "about": "Renamed by the model, so not kept."},
            {"test_name": "Urea", "about": ""},
            {"test_name": "Thyroid Stimulating Hormone", "about": "The pituitary's signal to the thyroid."},
        ]}])
        explained = LabTestExplainer(chat).explain(["HbA1c", "Cholesterol - LDL", "Urea", "Thyroid Stimulating Hormone -"])
        self.assertEqual(explained, {"HbA1c": "Average blood sugar over three months.",
                                     "Thyroid Stimulating Hormone -": "The pituitary's signal to the thyroid."})
        self.assertIn("- Cholesterol - LDL", str(chat.requests[0]))

    def test_a_model_failure_explains_nothing(self):
        from fakes import ScriptedChat

        from app.integrations.extraction import LabTestExplainer

        self.assertEqual(LabTestExplainer(ScriptedChat(["not json", "still not json"])).explain(["HbA1c"]), {})
