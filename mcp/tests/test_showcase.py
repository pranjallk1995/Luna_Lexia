import unittest

from fakes import FakeDocumentRepository
from app.services.showcase import ShowcaseSorter

PAYSLIP, PAYSLIP_2, POLICY, POLICY_2 = [1.0, 0.1, 0.0], [0.9, 0.4, 0.0], [0.0, 0.1, 1.0], [0.0, 0.5, 0.8]


class ShowcaseSorterTests(unittest.TestCase):
    """Documents with alike passages end up next to each other; newest-first comes back on request."""

    DOCUMENTS = {  # name: mean passage embedding, oldest first
        "payslip_1.pdf": PAYSLIP,
        "policy_1.pdf": POLICY,
        "payslip_2.pdf": PAYSLIP_2,
        "policy_2.pdf": POLICY_2,
        "payslip_3.pdf": PAYSLIP,
    }

    def setUp(self) -> None:
        self.repository = FakeDocumentRepository()
        for name, embedding in self.DOCUMENTS.items():
            self.add(name, embedding)
        self.sorter = ShowcaseSorter(self.repository, "test-model")

    def add(self, name: str, embedding: list[float] | None) -> None:
        self.repository.rows[name] = {
            "name": name, "embedding": embedding, "hidden": False, "favorite": False,
            "doc_type": "other", "financial_year": None,
        }

    def library(self) -> list[str]:
        return [row["name"] for row in self.repository.list()]

    def test_similarity_groups_alike_documents_from_the_newest(self) -> None:
        result = self.sorter.sort("similarity")
        order = self.library()
        self.assertEqual((result["document_count"], result["start"], order[0]), (5, "payslip_3.pdf", "payslip_3.pdf"))
        self.assertEqual(set(order[:3]), {"payslip_1.pdf", "payslip_2.pdf", "payslip_3.pdf"})
        self.assertEqual(set(order[3:]), {"policy_1.pdf", "policy_2.pdf"})

    def test_start_document_and_documents_without_passages(self) -> None:
        self.add("pending.pdf", None)
        result = self.sorter.sort("similarity", start="policy_1.pdf")
        self.assertEqual(result["unranked_without_embedding"], ["pending.pdf"])
        self.assertEqual(self.library()[:3], ["pending.pdf", "policy_1.pdf", "policy_2.pdf"])
        with self.assertRaises(FileNotFoundError):
            self.sorter.sort("similarity", start="pending.pdf")

    def test_newest_clears_the_saved_order(self) -> None:
        self.sorter.sort("similarity")
        self.assertEqual(self.sorter.sort("newest")["document_count"], 5)
        self.assertEqual(self.library(), list(self.DOCUMENTS))

    def test_arrival_is_placed_beside_its_closest_neighbors(self) -> None:
        self.sorter.sort("similarity")
        self.add("policy_3.pdf", POLICY)
        self.sorter.place("policy_3.pdf")
        self.assertEqual(set(self.library()[-3:]), {"policy_1.pdf", "policy_2.pdf", "policy_3.pdf"})

    def test_placing_does_nothing_while_newest_first(self) -> None:
        self.add("new.pdf", PAYSLIP)
        self.sorter.place("new.pdf")
        self.assertIsNone(self.repository.rows["new.pdf"].get("showcase_rank"))

    def test_hiding_drops_the_rank(self) -> None:
        self.sorter.sort("similarity")
        self.repository.set_hidden(["payslip_1.pdf"], True)
        self.assertIsNone(self.repository.rows["payslip_1.pdf"]["showcase_rank"])
