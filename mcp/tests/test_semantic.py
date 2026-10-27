"""Tests for passages, background embedding, retrieval, and answers."""

import unittest
from unittest.mock import MagicMock, patch

import httpx

from app.services.semantic import Chunker, SemanticIndex

DOCUMENT = {
    "document_id": "doc-1",
    "name": "march.pdf",
    "title": "Payslip for March 2025",
    "summary": "Monthly salary slip from Acme Corp.",
    "doc_type": "payslip",
    "financial_year": "2024-25",
    "issuer": "Acme Corp",
    "document_date": "2025-03-31",
    "extracted": {"net_pay": 80000, "employee": {"name": "Asha Rao", "pan": None}, "earnings": [{"label": "Basic", "amount": 40000}]},
    "page_texts": ["ACME CORP PAYSLIP\nNet Pay 80,000", "Page two text"],
    "fingerprint": "f" * 32,
}


class FakeChunks:
    def __init__(self, documents):
        self.documents = list(documents)
        self.stored = {}
        self.search_args = None
        self.rows = []

    def pending(self, model, limit):
        return [d for d in self.documents if self.stored.get(d["document_id"], (None,))[0] != d["fingerprint"]][:limit]

    def replace(self, document_id, fingerprint, model, chunks):
        self.stored[document_id] = (fingerprint, chunks)

    def forget(self, names=None):
        self.stored.clear()
        return 3

    def status(self, model):
        return {"documents": len(self.documents), "indexed": len(self.stored), "chunks": 3}

    def search(self, *args, any_word=False):
        self.search_args = args
        self.any_word = any_word
        return self.rows


class FakeEmbedder:
    model, dimensions = "embed-test", 3

    def __init__(self, fail=False):
        self.fail = fail
        self.inputs = []
        self.queries = []

    def passages(self, texts):
        if self.fail:
            raise httpx.ConnectError("ollama down")
        self.inputs += texts
        return [[1.0, 0.0, 0.0] for _ in texts]

    def query(self, question):
        self.queries.append(question)
        return [0.0, 1.0, 0.0]


def row(page=1, content="Net Pay 80,000", semantic_rank=1, keyword_rank=None):
    return {"score": 0.0164, "similarity": 0.83, "semantic_rank": semantic_rank, "keyword_rank": keyword_rank,
            "chunk_id": 1, "page": page, "content": content, "name": "march.pdf", "title": "Payslip for March 2025",
            "doc_type": "payslip", "financial_year": "2024-25", "issuer": "Acme Corp"}


def index(chunks, embedder=None):
    return SemanticIndex(chunks, embedder or FakeEmbedder(), Chunker(size=40, overlap=10), "http://ollama", "answer-model", 4096)


class ChunkerTests(unittest.TestCase):
    def test_overview_carries_the_extracted_details(self):
        overview = Chunker().overview(DOCUMENT)
        self.assertIn("Type: Payslip", overview)
        self.assertIn("net pay: 80000", overview)
        self.assertIn("employee name: Asha Rao", overview)
        self.assertIn("earnings 1 amount: 40000", overview)
        self.assertNotIn("pan", overview)

    def test_long_pages_become_overlapping_windows_cut_at_lines(self):
        text = "\n".join(f"line {n} of the statement" for n in range(12))
        windows = Chunker(size=80, overlap=20).windows(text)
        self.assertGreater(len(windows), 2)
        self.assertTrue(all(len(window) <= 80 for window in windows))
        self.assertIn("line 11", windows[-1])
        joined = " ".join(windows)
        self.assertTrue(all(f"line {n} " in joined for n in range(12)))

    def test_table_padding_and_rules_are_dropped(self):
        table = "| Gross Earning     |      80,000 |\n|-------------------|-------------|\n| Tax |   4,830 |"
        self.assertEqual(Chunker.compact(table), "| Gross Earning | 80,000 |\n\n| Tax | 4,830 |")

    def test_passages_number_pages_and_embed_with_their_document(self):
        chunker = Chunker()
        passages = chunker.passages(DOCUMENT)
        self.assertEqual([p.page for p in passages], [0, 1, 2])
        self.assertTrue(chunker.embedding_input(DOCUMENT, passages[1]).startswith("Payslip for March 2025 (Payslip, FY 2024-25), page 1\n"))
        self.assertEqual(chunker.passages({**DOCUMENT, "page_texts": ["", "  "]})[1:], [])


class SyncTests(unittest.TestCase):
    def test_pending_documents_are_embedded_once(self):
        chunks, embedder = FakeChunks([DOCUMENT]), FakeEmbedder()
        semantic = index(chunks, embedder)
        self.assertEqual(semantic.sync()["embedded"], ["march.pdf"])
        fingerprint, stored = chunks.stored["doc-1"]
        self.assertEqual(fingerprint, DOCUMENT["fingerprint"])
        self.assertEqual(stored[0][0], 0)
        self.assertEqual(len(stored), len(embedder.inputs))
        self.assertEqual(semantic.sync()["embedded"], [])

    def test_an_unreachable_model_is_reported_not_raised(self):
        semantic = index(FakeChunks([DOCUMENT]), FakeEmbedder(fail=True))
        self.assertEqual(semantic.sync(), {"embedded": [], "failed": ["march.pdf"]})
        self.assertIn("ollama down", semantic.status()["last_error"])

    def test_reindex_forgets_and_wakes_the_worker(self):
        semantic = index(FakeChunks([DOCUMENT]))
        self.assertEqual(semantic.reindex(["march.pdf"])["passages_removed"], 3)
        self.assertTrue(semantic._wake.is_set())
        self.assertIn("cosine", semantic.status()["similarity"])


class RetrievalTests(unittest.TestCase):
    def test_hybrid_search_embeds_the_question_and_uses_both_rankings(self):
        chunks, embedder = FakeChunks([]), FakeEmbedder()
        chunks.rows = [row(keyword_rank=2)]
        hits = index(chunks, embedder).search("net pay in March?", 5, ["payslip"], "2024-25")
        vector, text, model, doc_types, year, candidates, limit, k = chunks.search_args
        self.assertEqual((vector, text, model, doc_types, year, limit, k), ([0.0, 1.0, 0.0], "net pay in March?", "embed-test#passages-v2", ["payslip"], "2024-25", 5, 60))
        self.assertEqual(hits[0]["matched_by"], ["semantic", "keyword"])
        self.assertTrue(chunks.any_word)
        self.assertEqual((hits[0]["passage"], hits[0]["similarity"]), ("page 1", 0.83))

    def test_modes_choose_the_rankings(self):
        chunks, embedder = FakeChunks([]), FakeEmbedder()
        semantic = index(chunks, embedder)
        semantic.search("AAAPL1234C", mode="keyword")
        self.assertEqual((chunks.search_args[0], chunks.search_args[1], embedder.queries), (None, "AAAPL1234C", []))
        self.assertFalse(chunks.any_word)
        semantic.search("salary", mode="semantic")
        self.assertEqual(chunks.search_args[1], "")
        with self.assertRaisesRegex(ValueError, "mode must be"):
            semantic.search("x", mode="fuzzy")
        with self.assertRaisesRegex(ValueError, "Give a question"):
            semantic.search("  ")

    def test_overview_passages_are_labelled(self):
        chunks = FakeChunks([])
        chunks.rows = [row(page=0, content="Title: Payslip")]
        hit = index(chunks).search("payslip")[0]
        self.assertEqual((hit["passage"], hit["page"]), ("overview", None))

    def test_ask_answers_from_numbered_sources(self):
        chunks = FakeChunks([])
        chunks.rows = [row(), row(page=0, content="Title: Payslip")]
        reply = MagicMock()
        reply.json.return_value = {"message": {"content": "Net pay was 80,000 [1]."}}
        client = MagicMock()
        client.__enter__.return_value.post.return_value = reply
        with patch("app.services.semantic.httpx.Client", return_value=client):
            result = index(chunks).ask("What was my net pay in March?")
        prompt = client.__enter__.return_value.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("[1] Payslip for March 2025 (march.pdf, page 1)", prompt)
        self.assertIn("[2] Payslip for March 2025 (march.pdf, overview)", prompt)
        self.assertEqual(result["answer"], "Net pay was 80,000 [1].")
        self.assertTrue(result["cited"])
        self.assertEqual([source["number"] for source in result["sources"]], [1, 2])

    def test_ask_without_matches_does_not_call_the_model(self):
        with patch("app.services.semantic.httpx.Client") as client:
            result = index(FakeChunks([])).ask("Anything?")
        client.assert_not_called()
        self.assertEqual(result["sources"], [])


if __name__ == "__main__":
    unittest.main()
