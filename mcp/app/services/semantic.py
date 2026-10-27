"""Retrieval-augmented generation over the vault: passages, embeddings, search, and answers.

Every document becomes passages: one overview (title, type, issuer, year,
summary, and the extracted details) and overlapping windows of each page's
parsed text. A background worker embeds them with Luna Sentia's embedding model
into pgvector whenever a document's content changes. Search ranks passages by
cosine similarity, by full-text match, or by both fused with Reciprocal Rank
Fusion; ``ask`` hands the best passages to the local model to answer with
citations. Hidden documents are never searched.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.schemas import DOCUMENT_TYPES
from app.data.repositories import ChunkRepository, Row
from app.integrations.embeddings import OllamaEmbedder

logger = logging.getLogger(__name__)

MODES = ("hybrid", "semantic", "keyword")


@dataclass(frozen=True)
class Passage:
    page: int  # 0 for the overview passage
    content: str


class Chunker:
    """Split a document into an overview passage and overlapping page windows."""

    # Bump when passages are built differently, so every document is embedded again.
    VERSION = 2
    PADDING = re.compile(r"[ \t]{2,}")
    RULE_LINE = re.compile(r"^\s*\|?[\s:|-]*-{3,}[\s:|-]*\|?\s*$", re.M)

    def __init__(self, size: int = 1200, overlap: int = 200) -> None:
        self.size = size
        self.overlap = overlap

    @staticmethod
    def label(doc_type: str) -> str:
        kind = DOCUMENT_TYPES.get(doc_type)
        return kind.label if kind else doc_type.replace("_", " ")

    @classmethod
    def flatten(cls, value: Any, prefix: str = "") -> list[str]:
        """Extracted details as readable ``field: value`` lines, skipping empty ones."""
        if isinstance(value, dict):
            return [line for key, item in value.items() for line in cls.flatten(item, f"{prefix}{key.replace('_', ' ')} ")]
        if isinstance(value, list):
            return [line for index, item in enumerate(value, 1) for line in cls.flatten(item, f"{prefix}{index} ")]
        if value in (None, "", [], {}):
            return []
        return [f"{prefix.strip()}: {value}"]

    def overview(self, document: Row) -> str:
        lines = [
            f"Title: {document['title']}",
            f"Type: {self.label(document['doc_type'])}",
            f"Issuer: {document.get('issuer') or 'unknown'}",
            f"Financial year: {document.get('financial_year') or 'unknown'}",
            f"Document date: {document.get('document_date') or 'unknown'}",
            f"Summary: {document['summary']}",
        ]
        details = self.flatten(document.get("extracted") or {})
        if details:
            lines += ["Extracted details:", *details]
        return "\n".join(lines)

    @classmethod
    def compact(cls, text: str) -> str:
        """Drop Markdown table rules and the space padding of table cells."""
        return cls.PADDING.sub(" ", cls.RULE_LINE.sub("", text)).strip()

    def windows(self, text: str) -> list[str]:
        """Windows of about ``size`` characters, cut at line breaks where possible."""
        text = self.compact(text)
        if len(text) <= self.size:
            return [text] if text else []
        windows, start = [], 0
        while start < len(text):
            end = min(start + self.size, len(text))
            if end < len(text):
                cut = text.rfind("\n", start + self.size // 2, end)
                end = cut if cut > start else end
            windows.append(text[start:end].strip())
            if end >= len(text):
                break
            start = max(end - self.overlap, start + 1)
        return [window for window in windows if window]

    def passages(self, document: Row) -> list[Passage]:
        passages = [Passage(0, self.overview(document))]
        for page, text in enumerate(document.get("page_texts") or [], 1):
            passages += [Passage(page, window) for window in self.windows(text)]
        return passages

    def embedding_input(self, document: Row, passage: Passage) -> str:
        """Each passage is embedded with a line naming its document, so it carries its context."""
        where = "overview" if passage.page == 0 else f"page {passage.page}"
        year = f", FY {document['financial_year']}" if document.get("financial_year") else ""
        return f"{document['title']} ({self.label(document['doc_type'])}{year}), {where}\n{passage.content}"


class SemanticIndex:
    """Keep passages embedded, and answer search and questions from them."""

    POLL_SECONDS = 10
    BATCH_DOCUMENTS = 8
    SNIPPET_CHARS = 1500
    ANSWER_INSTRUCTIONS = (
        "You answer questions about the user's own documents using only the numbered sources. "
        "Cite every fact with its source number in brackets, like [2]. Copy amounts, dates, and "
        "identifiers exactly as written, and say which document and period each figure comes from "
        "(for example one month's payslip versus a whole year's Form 16). Do not add figures "
        "together unless the question asks for a total and every needed figure is in the sources. "
        "If the sources do not contain the answer, say so plainly instead of guessing."
    )
    CITATION = re.compile(r"\[\d+\]")

    def __init__(
        self,
        chunks: ChunkRepository,
        embedder: OllamaEmbedder,
        chunker: Chunker,
        ollama_url: str,
        answer_model: str,
        context_tokens: int,
        candidates: int = 40,
        rrf_k: int = 60,
    ) -> None:
        self.chunks = chunks
        self.embedder = embedder
        self.chunker = chunker
        self.ollama_url = ollama_url
        self.answer_model = answer_model
        self.context_tokens = context_tokens
        self.candidates = candidates
        self.rrf_k = rrf_k
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self.last_error: str | None = None
        # Passages are stored under the model and passage version that made them.
        self.tag = f"{embedder.model}#passages-v{Chunker.VERSION}"

    # Indexing

    def sync(self) -> dict[str, Any]:
        """Embed documents whose passages are missing or out of date, a batch at a time."""
        embedded, failed = [], []
        while True:
            pending = self.chunks.pending(self.tag, self.BATCH_DOCUMENTS)
            pending = [document for document in pending if document["name"] not in failed]
            if not pending:
                break
            for document in pending:
                try:
                    passages = self.chunker.passages(document)
                    vectors = self.embedder.passages(
                        [self.chunker.embedding_input(document, passage) for passage in passages]
                    )
                    self.chunks.replace(
                        str(document["document_id"]),
                        document["fingerprint"],
                        self.tag,
                        [(passage.page, passage.content, vector) for passage, vector in zip(passages, vectors)],
                    )
                    embedded.append(document["name"])
                except (httpx.HTTPError, ValueError) as error:
                    self.last_error = f"{document['name']}: {error}"[:500]
                    logger.warning("Could not embed %s: %s", document["name"], error)
                    failed.append(document["name"])
            if failed and len(failed) >= len(pending):
                break  # Everything left is failing, most likely Ollama; retry on the next poll.
        if embedded and not failed:
            self.last_error = None
        return {"embedded": embedded, "failed": failed}

    def start(self) -> None:
        """Embed in the background now, and again every few seconds or when woken."""
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="lexia-embeddings", daemon=True)
            self._worker.start()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while True:
            try:
                self.sync()
            except Exception as error:  # noqa: BLE001 - the worker must survive a database or model outage
                self.last_error = str(error)[:500]
                logger.exception("Embedding sync failed")
            self._wake.wait(self.POLL_SECONDS)
            self._wake.clear()

    def reindex(self, names: list[str] | None = None) -> dict[str, Any]:
        removed = self.chunks.forget(names)
        self.wake()
        return {"status": "queued", "passages_removed": removed, "documents": names or "all"}

    def status(self) -> dict[str, Any]:
        counts = self.chunks.status(self.tag)
        return {
            "embedding_model": self.embedder.model,
            "dimensions": self.embedder.dimensions,
            "documents": counts["documents"],
            "documents_indexed": counts["indexed"],
            "documents_pending": counts["documents"] - counts["indexed"],
            "passages": counts["chunks"],
            "similarity": "cosine similarity (1 - pgvector cosine distance) over an HNSW index",
            "hybrid_ranking": f"Reciprocal Rank Fusion of vector and full-text ranks, k={self.rrf_k}",
            "last_error": self.last_error,
        }

    # Retrieval

    def search(
        self,
        query: str,
        limit: int = 8,
        doc_types: list[str] | None = None,
        financial_year: str | None = None,
        mode: str = "hybrid",
    ) -> list[Row]:
        query = query.strip()
        if not query:
            raise ValueError("Give a question or phrase to search for.")
        if mode not in MODES:
            raise ValueError(f"mode must be one of: {', '.join(MODES)}.")
        limit = max(1, min(int(limit), 50))
        vector = self.embedder.query(query) if mode in ("hybrid", "semantic") else None
        rows = self.chunks.search(
            vector,
            query if mode in ("hybrid", "keyword") else "",
            self.tag,
            doc_types,
            financial_year,
            max(self.candidates, limit),
            limit,
            self.rrf_k,
            any_word=mode == "hybrid",
        )
        return [
            {
                "document": row["name"],
                "title": row["title"],
                "doc_type": row["doc_type"],
                "financial_year": row["financial_year"],
                "page": row["page"] or None,
                "passage": "overview" if row["page"] == 0 else f"page {row['page']}",
                "score": round(float(row["score"]), 5),
                "similarity": None if row["similarity"] is None else round(float(row["similarity"]), 4),
                "matched_by": [name for name, rank in (("semantic", row["semantic_rank"]), ("keyword", row["keyword_rank"])) if rank],
                "text": row["content"][: self.SNIPPET_CHARS],
            }
            for row in rows
        ]

    def ask(
        self,
        question: str,
        sources: int = 6,
        doc_types: list[str] | None = None,
        financial_year: str | None = None,
    ) -> dict[str, Any]:
        """Answer from the best passages with the local model, citing them as [n]."""
        hits = self.search(question, max(1, min(int(sources), 12)), doc_types, financial_year, "hybrid")
        if not hits:
            return {"answer": "No document passages match this question.", "sources": []}
        numbered = "\n\n".join(
            f"[{index}] {hit['title']} ({hit['document']}, {hit['passage']})\n{hit['text']}"
            for index, hit in enumerate(hits, 1)
        )
        payload = {
            "model": self.answer_model,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.context_tokens},
            "messages": [
                {"role": "system", "content": self.ANSWER_INSTRUCTIONS},
                {"role": "user", "content": f"Sources:\n\n{numbered}\n\nQuestion: {question}"},
            ],
        }
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
            response = client.post(f"{self.ollama_url}/api/chat", json=payload)
            response.raise_for_status()
        answer = response.json()["message"]["content"].strip()
        return {
            "answer": answer,
            "cited": bool(self.CITATION.search(answer)),
            "model": self.answer_model,
            "sources": [
                {"number": index, **{key: hit[key] for key in ("document", "title", "passage", "page", "score", "similarity")}}
                for index, hit in enumerate(hits, 1)
            ],
        }
