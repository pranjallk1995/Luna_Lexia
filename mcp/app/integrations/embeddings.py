"""Text embeddings from Luna Sentia's Ollama, for semantic search over documents."""

from __future__ import annotations

import httpx


class OllamaEmbedder:
    """Embed passages and questions with one Ollama embedding model.

    Qwen3 embedding models are instruction-aware: a question is embedded with a
    one-line task description in front, while stored passages are embedded as
    they are. Ollama returns unit-length vectors, so cosine similarity is their
    dot product.
    """

    BATCH = 16
    QUERY_TASK = "Given a question about personal financial or identity documents, retrieve passages that answer it"

    def __init__(self, base_url: str, model: str, dimensions: int, timeout: float = 120.0) -> None:
        self.base_url = base_url
        self.model = model
        self.dimensions = dimensions
        self.timeout = timeout

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
            for start in range(0, len(texts), self.BATCH):
                response = client.post(
                    f"{self.base_url}/api/embed",
                    json={"model": self.model, "input": texts[start : start + self.BATCH], "truncate": True},
                )
                response.raise_for_status()
                vectors.extend(response.json()["embeddings"])
        for vector in vectors:
            if len(vector) != self.dimensions:
                raise ValueError(
                    f"{self.model} returned {len(vector)}-dimensional vectors; EMBEDDING_DIMENSIONS is {self.dimensions}."
                )
        return vectors

    def passages(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts) if texts else []

    def query(self, question: str) -> list[float]:
        return self._embed([f"Instruct: {self.QUERY_TASK}\nQuery: {question}"])[0]
