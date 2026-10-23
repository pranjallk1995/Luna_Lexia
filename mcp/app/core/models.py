"""Validated metadata models and normalization rules for Luna Lexia."""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from app.core.schemas import DocTypeName, KeyFacts


class DocumentClassification(BaseModel):
    """Validated type, title, summary, and tags returned by the document model."""

    doc_type: DocTypeName
    title: str = Field(min_length=3, max_length=120)
    summary: str = Field(min_length=3, max_length=1000)
    tags: list[str] = Field(min_length=1, max_length=16)
    confidence: float = Field(ge=0, le=1)


@dataclass(frozen=True)
class DocumentAnalysis:
    """Everything the model produced for one document."""

    doc_type: str
    title: str
    summary: str
    tags: list[str]
    confidence: float
    extracted: dict[str, Any]
    facts: KeyFacts
    review_flags: list[str] = field(default_factory=list)
    extraction_error: str | None = None


@dataclass(frozen=True)
class DocumentContent:
    """The pages of a document as Markdown text (with OCR and tables), one entry per page."""

    file_format: str
    page_texts: list[str]
    parser: str

    # Below this many letters and digits the text is too thin to analyze on its own.
    MIN_TEXT_CHARACTERS = 40

    @property
    def page_count(self) -> int:
        return len(self.page_texts)

    @property
    def has_text(self) -> bool:
        return len(re.findall(r"\w", "".join(self.page_texts))) >= self.MIN_TEXT_CHARACTERS

    def text(self, max_chars: int | None = None) -> str:
        """Join pages under ``--- Page N ---`` markers, cut to ``max_chars``."""
        text = "\n".join(
            f"--- Page {number} ---\n{page.strip()}"
            for number, page in enumerate(self.page_texts, start=1)
        )
        return text if max_chars is None else text[:max_chars]


@dataclass(frozen=True)
class DocumentRecord:
    """Content identity, analysis results, and derived facts for one stored document."""

    name: str
    size_bytes: int
    content_sha256: str
    file_format: str
    page_count: int
    doc_type: str
    title: str
    summary: str
    tags: list[str]
    confidence: float
    extracted: dict[str, Any]
    extraction_error: str | None
    review_flags: list[str]
    financial_year: str | None
    document_date: date | None
    issuer: str | None
    primary_amount: float | None
    page_texts: list[str]
    parser: str
    analysis_model: str
    schema_version: int


@dataclass(frozen=True)
class MetadataChange:
    """An explicit title/summary edit, tag changes, corrected extracted fields, and/or
    confirmation that a person reviewed the details (which clears review flags)."""

    title: str | None = None
    summary: str | None = None
    add_tags: list[str] | None = None
    remove_tags: list[str] | None = None
    extracted: dict[str, Any] | None = None
    mark_reviewed: bool = False

    @property
    def is_empty(self) -> bool:
        return (
            self.title is None
            and self.summary is None
            and self.add_tags is None
            and self.remove_tags is None
            and not self.extracted
            and not self.mark_reviewed
        )


class MetadataRules:
    """Normalize titles, summaries, tags, and search text consistently across use cases."""

    TITLE_MIN_LENGTH = 3
    TITLE_MAX_LENGTH = 120
    SUMMARY_MIN_LENGTH = 3
    SUMMARY_MAX_LENGTH = 1000
    TAG_MAX_LENGTH = 60
    REVIEW_TAG = "needs review"

    def __init__(self, max_tags: int, search_stop_words: frozenset[str]) -> None:
        self.max_tags = max_tags
        self.search_stop_words = search_stop_words

    @staticmethod
    def require_lowercase_tags(tags: list[str] | None, field: str) -> list[str] | None:
        """Reject tags containing capital letters instead of silently lowercasing them."""
        capitalized = [tag for tag in tags or [] if tag != tag.lower()]
        if capitalized:
            suggestions = ", ".join(repr(tag.lower()) for tag in capitalized)
            raise ValueError(
                f"{field} must be lowercase; capital letters are not allowed in tags. "
                f"Use {suggestions} instead."
            )
        return tags

    def normalize_tags(self, tags: list[str]) -> list[str]:
        """Normalize, deduplicate, and bound search tags."""
        normalized = []
        for tag in tags:
            clean = re.sub(r"[^a-z0-9 -]+", "", tag.lower()).strip()
            clean = re.sub(r"\s+", " ", clean)
            if clean and clean not in normalized:
                normalized.append(clean[: self.TAG_MAX_LENGTH])
        if not normalized:
            raise ValueError("Document analysis returned no usable tags.")
        return normalized[: self.max_tags]

    @staticmethod
    def _bounded(value: str, field: str, minimum: int, maximum: int) -> str:
        text = re.sub(r"\s+", " ", value).strip()
        if not minimum <= len(text) <= maximum:
            raise ValueError(f"{field} must contain between {minimum} and {maximum} characters.")
        return text

    def title(self, value: str) -> str:
        return self._bounded(value, "Title", self.TITLE_MIN_LENGTH, self.TITLE_MAX_LENGTH)

    def summary(self, value: str) -> str:
        return self._bounded(value, "Summary", self.SUMMARY_MIN_LENGTH, self.SUMMARY_MAX_LENGTH)

    def edited_tags(
        self,
        current: list[str],
        add_tags: list[str] | None,
        remove_tags: list[str] | None,
    ) -> list[str]:
        """Apply tag removals then additions while keeping order and bounds."""
        additions = self.normalize_tags(add_tags) if add_tags else []
        removals = set(self.normalize_tags(remove_tags)) if remove_tags else set()
        updated = [tag for tag in current if tag not in removals]
        for tag in additions:
            if tag not in updated:
                updated.append(tag)
        if not updated:
            raise ValueError("At least one tag must remain on the document.")
        if len(updated) > self.max_tags:
            raise ValueError(f"Documents can have at most {self.max_tags} tags.")
        return updated

    def with_review_tag(self, tags: list[str], flags: list[str]) -> list[str]:
        """Keep the ``needs review`` tag exactly when there are review flags."""
        tags = [tag for tag in tags if tag != self.REVIEW_TAG]
        return self.with_tags(tags, [self.REVIEW_TAG]) if flags else tags

    def with_tags(self, tags: list[str], extra: list[str]) -> list[str]:
        """Append system tags, dropping trailing other tags to stay within the limit."""
        extra = [tag for tag in extra if tag not in tags]
        return [*tags[: max(self.max_tags - len(extra), 0)], *extra]

    def search_query(self, query: str) -> str:
        """Reduce natural-language search text to meaningful indexed words."""
        words = re.findall(r"[a-z0-9]+", query.lower())
        return " ".join(word for word in words if word not in self.search_stop_words)
