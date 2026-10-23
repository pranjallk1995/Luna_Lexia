"""Luna Lexia use cases, independent of the HTTP and MCP transports."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.config import AppConfig
from app.core.models import (
    DocumentAnalysis,
    DocumentContent,
    DocumentRecord,
    MetadataChange,
    MetadataRules,
)
from app.core.schemas import (
    DOCUMENT_TYPES,
    SCHEMA_VERSION,
    KeyFacts,
    document_type,
    parse_year_range,
)
from app.data.repositories import DocumentRepository, PublicURLRepository, Row
from app.data.storage import DocumentStorage
from app.integrations.extraction import DocumentAnalyzer
from app.integrations.parsing import DoclingParser
from app.services.documents import DocumentPayloadDecoder, DocumentReader, PreviewRenderer
from app.services.downloads import DownloadStore, DownloadTicket
from app.services.uploads import UploadLinkStore


# The compact view of a document returned to LLM clients when listing or searching.
SUMMARY_FIELDS = (
    "name",
    "doc_type",
    "title",
    "financial_year",
    "document_date",
    "issuer",
    "primary_amount",
    "tags",
    "favorite",
    "page_count",
    "size_bytes",
    "updated_at",
    "public_url",
)

# The summary plus the extracted details, for bulk reading by tax and finance tools.
DATA_FIELDS = (*SUMMARY_FIELDS, "summary", "extracted", "review_flags", "extraction_error")

# A preview's text key only needs enough to identify, describe, and link each tile.
PREVIEW_FIELDS = ("name", "doc_type", "title", "public_url")


def summarize(row: Row, fields: tuple[str, ...] = SUMMARY_FIELDS) -> Row:
    return {key: row[key] for key in fields if key in row}


def page(items: list[Row], total: int, limit: int, offset: int) -> dict[str, Any]:
    """Wrap one page of results with the offset of the next page, if any."""
    next_offset = offset + len(items)
    return {
        "count": total,
        "offset": offset,
        "next_offset": next_offset if next_offset < total else None,
        "documents": items,
    }


def unique_names(names: list[str], empty_message: str) -> list[str]:
    """Return ``names`` deduplicated in order, rejecting an empty selection."""
    if not isinstance(names, list) or not names:
        raise ValueError(empty_message)
    if any(not isinstance(name, str) for name in names):
        raise ValueError("Every document name must be a string.")
    return list(dict.fromkeys(names))


@dataclass(frozen=True)
class DocumentFilter:
    """Optional type and financial-year restrictions, validated once."""

    doc_types: list[str] | None = None
    financial_year: str | None = None

    @classmethod
    def create(cls, doc_types: list[str] | None = None, financial_year: str | None = None) -> DocumentFilter:
        for key in doc_types or []:
            document_type(key)
        year = None
        if financial_year:
            year = parse_year_range(financial_year)
            if year is None:
                raise ValueError('financial_year must look like "2024-25".')
        return cls(list(dict.fromkeys(doc_types)) if doc_types else None, year)


class PublicURLService:
    """Validate and persist the HTTPS origin that every document URL derives from."""

    def __init__(self, repository: PublicURLRepository) -> None:
        self.repository = repository

    @staticmethod
    def validate(public_base_url: str) -> str:
        """Normalize and validate a public HTTPS origin and optional path prefix."""
        normalized = public_base_url.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "public_base_url must be an HTTPS URL without credentials, "
                "a query, or a fragment."
            )
        return normalized

    def seed(self, default_url: str) -> str:
        return self.repository.seed(default_url)

    def base_url(self) -> str:
        public_base_url = self.repository.get()
        if not public_base_url:
            raise RuntimeError("The Luna Lexia public base URL is not configured.")
        return public_base_url

    def update(self, public_base_url: str) -> dict[str, Any]:
        """Persist a new HTTPS origin and regenerate all stored document URLs."""
        normalized = self.validate(public_base_url)
        return {
            "status": "public_base_url_updated",
            "public_base_url": normalized,
            "updated_document_count": self.repository.replace(normalized),
        }


class DocumentIndexer:
    """Parse, analyze, and validate document bytes, then persist the result.

    The pipeline is Docling (text, OCR, tables) -> classification and
    extraction agents -> schema and business-rule validation -> PostgreSQL.
    """

    def __init__(
        self,
        storage: DocumentStorage,
        documents: DocumentRepository,
        parser: DoclingParser,
        analyzer: DocumentAnalyzer,
        urls: PublicURLService,
        rules: MetadataRules,
    ) -> None:
        self.storage = storage
        self.documents = documents
        self.parser = parser
        self.analyzer = analyzer
        self.reader = analyzer.reader
        self.urls = urls
        self.rules = rules

    def system_tags(self, analysis: DocumentAnalysis) -> list[str]:
        """Tag every document with its type and financial year for easy filtering."""
        tags = []
        if DOCUMENT_TYPES[analysis.doc_type].tag:
            tags.append(DOCUMENT_TYPES[analysis.doc_type].tag)
        if analysis.facts.financial_year:
            tags.append(f"fy {analysis.facts.financial_year}")
        return tags

    def analyze(self, data: bytes, doc_type: str | None = None) -> tuple[DocumentAnalysis, DocumentContent]:
        """Parse and analyze bytes; return the analysis and the pages it was read from."""
        content = self.parser.parse(data, self.reader.file_format(data))
        analysis = self.analyzer.analyze(data, content, doc_type)
        tags = self.rules.with_tags(analysis.tags, self.system_tags(analysis))
        return replace(analysis, tags=self.rules.with_review_tag(tags, analysis.review_flags)), content

    def store(self, name: str, data: bytes, analysis: DocumentAnalysis, content: DocumentContent) -> Row:
        record = DocumentRecord(
            name=name,
            size_bytes=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(),
            file_format=content.file_format,
            page_count=content.page_count,
            doc_type=analysis.doc_type,
            title=analysis.title,
            summary=analysis.summary,
            tags=list(analysis.tags),
            confidence=analysis.confidence,
            extracted=analysis.extracted,
            extraction_error=analysis.extraction_error,
            review_flags=list(analysis.review_flags),
            financial_year=analysis.facts.financial_year,
            document_date=analysis.facts.document_date,
            issuer=analysis.facts.issuer,
            primary_amount=analysis.facts.amount,
            page_texts=content.page_texts,
            parser=content.parser,
            analysis_model=self.analyzer.model,
            schema_version=SCHEMA_VERSION,
        )
        return self.documents.upsert(record, self.urls.repository.get())

    def backfill(self) -> dict[str, Any]:
        """Analyze unindexed, changed, or outdated files while skipping current records.

        A record is current when its hash, model, and schema version all match.
        A failing file is reported and the run continues with the next one.
        """
        indexed = {item["name"]: item for item in self.documents.list(hidden=None)}
        analyzed, failed = [], []
        skipped = 0
        for path in self.storage.document_files():
            data = path.read_bytes()
            existing = indexed.get(path.name)
            if (
                existing
                and existing["content_sha256"] == hashlib.sha256(data).hexdigest()
                and existing["analysis_model"] == self.analyzer.model
                and existing["schema_version"] == SCHEMA_VERSION
            ):
                skipped += 1
                continue
            try:
                doc_type = existing["doc_type"] if existing else None
                self.store(path.name, data, *self.analyze(data, doc_type))
            except (OSError, ValueError, RuntimeError) as error:
                # PDFium reports unreadable files as RuntimeError subclasses.
                failed.append({"name": path.name, "error": str(error)})
                continue
            analyzed.append(path.name)
        return {
            "analyzed_count": len(analyzed),
            "skipped_count": skipped,
            "failed_count": len(failed),
            "analyzed": analyzed,
            "failed": failed,
        }


class DocumentCollectionService:
    """Operations shared by the visible library and the Hidden Vault.

    Each subclass is bound to one side of the ``hidden`` flag and refuses to
    touch documents that belong to the other side.
    """

    HIDDEN: bool
    WRONG_COLLECTION_MESSAGE: str

    def __init__(
        self,
        storage: DocumentStorage,
        documents: DocumentRepository,
        rules: MetadataRules,
        indexer: DocumentIndexer,
    ) -> None:
        self.storage = storage
        self.documents = documents
        self.rules = rules
        self.indexer = indexer

    def path(self, name: str) -> Path:
        """Return the file for a document in this collection or reject access."""
        path = self.storage.existing_path(name)
        if self.documents.is_hidden(name) != self.HIDDEN:
            raise PermissionError(self.WRONG_COLLECTION_MESSAGE)
        return path

    def get(self, name: str) -> Row:
        row = self.documents.get(name, hidden=self.HIDDEN)
        if row is None:
            raise FileNotFoundError(f"Document {name!r} was not found.")
        return row

    def list(self, doc_types: list[str] | None = None, financial_year: str | None = None) -> list[Row]:
        selection = DocumentFilter.create(doc_types, financial_year)
        return self.documents.list(self.HIDDEN, selection.doc_types, selection.financial_year)

    def update_metadata(self, name: str, change: MetadataChange) -> Row:
        """Edit the title, summary, tags, and/or individual extracted fields.

        Extracted fields are merged into the current details and revalidated
        against the document type's schema, so corrections follow the same
        rules as model output; the key facts and business-rule review flags
        are recomputed. ``mark_reviewed`` clears the review flags once a
        person has checked the details.
        """
        self.path(name)
        if change.is_empty:
            raise ValueError(
                "Provide a title, summary, add_tags, remove_tags, extracted, or mark_reviewed change."
            )
        current = self.get(name)
        title = current["title"] if change.title is None else self.rules.title(change.title)
        summary = current["summary"] if change.summary is None else self.rules.summary(change.summary)
        tags = self.rules.edited_tags(current["tags"], change.add_tags, change.remove_tags)
        schema = document_type(current["doc_type"]).schema
        extracted, error = current["extracted"], current["extraction_error"]
        if change.extracted:
            unknown = sorted(set(change.extracted) - set(schema.model_fields))
            if unknown:
                raise ValueError(
                    f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not "
                    f"{current['doc_type']} fields; see list_document_types."
                )
            extracted, error = {**extracted, **change.extracted}, None
        details = schema.model_validate(extracted)
        flags = current["review_flags"]
        if change.mark_reviewed:
            flags = []
        elif change.extracted:
            flags = details.review_flags()
        return self.documents.update_metadata(
            name,
            title,
            summary,
            self.rules.with_review_tag(tags, flags),
            details.model_dump(mode="json") if extracted else {},
            error,
            flags,
            details.key_facts() if extracted else self.facts_of(current),
            hidden=self.HIDDEN,
        )

    @staticmethod
    def facts_of(row: Row) -> KeyFacts:
        """Keep the stored key facts of a document that has no extracted details."""
        return KeyFacts(row["financial_year"], row["document_date"], row["issuer"], row["primary_amount"])

    def reextract(self, name: str, doc_type: str | None = None) -> Row:
        """Re-run classification and extraction, optionally forcing the document type."""
        path = self.path(name)
        if doc_type:
            document_type(doc_type)
        data = path.read_bytes()
        row = self.indexer.store(name, data, *self.indexer.analyze(data, doc_type))
        return {"status": "reextracted", **row}

    def read_text(self, name: str, first_page: int = 1, last_page: int | None = None, max_chars: int = 40_000) -> Row:
        """Return the parsed text (Markdown, with OCR and tables) of a page range."""
        self.path(name)
        page_texts = self.documents.pages(name, self.HIDDEN) or []
        page_count = len(page_texts)
        last_page = min(last_page or page_count, page_count)
        if not 1 <= first_page <= last_page:
            raise ValueError(f"Pages must be within 1-{page_count}.")
        pages, remaining, next_page = [], max_chars, None
        for number in range(first_page, last_page + 1):
            text = page_texts[number - 1].strip()
            if remaining <= 0:
                next_page = number
                break
            pages.append({"page": number, "text": text[:remaining]})
            remaining -= len(text)
        result: Row = {"name": name, "page_count": page_count, "pages": pages, "next_page": next_page}
        if not any(page["text"] for page in pages):
            result["note"] = "No text could be read from these pages; use view_document to see them."
        return result

    def remove(self, name: str) -> dict[str, str]:
        """Delete a document and its metadata, restoring the file on failure."""
        with self.storage.removing(self.path(name)):
            self.documents.delete(name)
        return {"status": "deleted", "removed_name": name}


class VisibleDocumentService(DocumentCollectionService):
    """The public library: ingest, rename, favorites, and public delivery."""

    HIDDEN = False
    WRONG_COLLECTION_MESSAGE = "Hidden documents require authentication."
    BATCH_MAX_DOCUMENTS = 100
    RENAME_STEM_PATTERN = re.compile(r"[A-Za-z0-9_]+")

    def __init__(
        self,
        storage: DocumentStorage,
        documents: DocumentRepository,
        rules: MetadataRules,
        indexer: DocumentIndexer,
        decoder: DocumentPayloadDecoder,
        max_fetch_documents: int,
    ) -> None:
        super().__init__(storage, documents, rules, indexer)
        self.decoder = decoder
        self.max_fetch_documents = max_fetch_documents

    def ingest(
        self,
        name: str,
        content_base64: str,
        replace: bool = False,
        password: str | None = None,
        doc_type: str | None = None,
    ) -> Row:
        """Create or replace a document from Base64 under exactly ``name``."""
        return self.ingest_bytes(
            name, self.decoder.decode_base64(content_base64), replace, password, doc_type
        )

    def ingest_bytes(
        self,
        name: str,
        data: bytes,
        replace: bool = False,
        password: str | None = None,
        doc_type: str | None = None,
    ) -> Row:
        """Create or replace a document and persist its analysis.

        A password-protected PDF is stored unlocked so it can be re-read later.
        """
        path = self.storage.path(name)
        if doc_type:
            document_type(doc_type)
        data = self.decoder.prepare(name, data, password)
        exists = path.is_file()
        if replace and not exists:
            raise FileNotFoundError(f"Document {name!r} was not found.")
        if replace and self.documents.is_hidden(name):
            raise PermissionError(self.WRONG_COLLECTION_MESSAGE)
        if not replace and exists:
            raise FileExistsError(f"Document {name!r} already exists; use replace_document.")
        analysis, content = self.indexer.analyze(data, doc_type)
        with self.storage.writing(path, data):
            return self.indexer.store(name, data, analysis, content)

    def upload(
        self,
        requested_name: str | None,
        data: bytes,
        password: str | None = None,
        doc_type: str | None = None,
    ) -> dict[str, Any]:
        """Add chat-supplied bytes, adapting the name instead of rejecting it.

        The extension follows the detected format, a taken name gets a numeric
        suffix, and bytes identical to a visible document are reported as a
        duplicate rather than stored twice (LLM clients often retry uploads).
        """
        detected = self.decoder.detect_format(data)
        if detected == "PDF":
            data = self.decoder.unlock(data, password)
        duplicate = self.documents.find_visible_by_content(hashlib.sha256(data).hexdigest())
        if duplicate is not None:
            return {
                "status": "duplicate",
                "message": f"This document is already stored as {duplicate['name']!r}.",
                **duplicate,
            }
        name = self.storage.available_name(self.decoder.name_for(requested_name, detected))
        result = {"status": "created", **self.ingest_bytes(name, data, doc_type=doc_type)}
        if requested_name and name != requested_name:
            result["requested_name"] = requested_name
        return result

    def rename(self, name: str, new_name: str) -> dict[str, Any]:
        """Rename a document, keeping its extension, immutable ID, and public URL."""
        source = self.path(name)
        destination = self.storage.path(new_name)
        if not self.RENAME_STEM_PATTERN.fullmatch(destination.stem):
            raise ValueError(
                "The new filename may contain only letters, numbers, and "
                "underscores before the file extension."
            )
        if source == destination:
            raise ValueError("The new document name must be different from the current name.")
        if source.suffix.lower() != destination.suffix.lower():
            raise ValueError("Renaming cannot change the document file extension.")
        if destination.exists():
            raise FileExistsError(f"Document {new_name!r} already exists.")
        with self.storage.renaming(source, destination):
            metadata = self.documents.rename(name, new_name)
        return {"status": "renamed", "old_name": name, **metadata}

    def details(self, name: str) -> dict[str, Any]:
        """Return the stored record, its extracted details, and the file's own properties."""
        path = self.path(name)
        return {**self.get(name), "file": self.indexer.reader.properties(path.read_bytes())}

    def data_page(
        self,
        limit: int,
        offset: int,
        max_limit: int,
        names: list[str] | None = None,
        doc_types: list[str] | None = None,
        financial_year: str | None = None,
    ) -> dict[str, Any]:
        """Return extracted details for a filtered page or an explicit batch of names."""
        if names is None:
            rows = self.list(doc_types, financial_year)
            limit, offset = min(max(limit, 1), max_limit), max(offset, 0)
            selected = rows[offset : offset + limit]
            return page([summarize(row, DATA_FIELDS) for row in selected], len(rows), limit, offset)
        clean_names = unique_names(names, "Provide at least one document name.")
        if len(clean_names) > max_limit:
            raise ValueError(f"Request details for at most {max_limit} documents at a time.")
        rows = [self.documents.get(name, hidden=False) for name in clean_names]
        result = page(
            [summarize(row, DATA_FIELDS) for row in rows if row is not None],
            len(clean_names),
            len(clean_names),
            0,
        )
        unavailable = [name for name, row in zip(clean_names, rows) if row is None]
        if unavailable:
            result["unavailable"] = unavailable
        return result

    def _apply_batch(
        self, items: list[tuple[str, Callable[[], Row]]], result_key: str
    ) -> dict[str, Any]:
        """Apply each change on its own, reporting failures instead of stopping."""
        if not items:
            raise ValueError("Provide at least one document change.")
        if len(items) > self.BATCH_MAX_DOCUMENTS:
            raise ValueError(f"Change at most {self.BATCH_MAX_DOCUMENTS} documents at a time.")
        succeeded, failed = [], []
        for name, apply in items:
            try:
                succeeded.append(apply())
            except (FileNotFoundError, FileExistsError, PermissionError, ValueError) as error:
                failed.append({"name": name, "error": str(error)})
        return {
            f"{result_key}_count": len(succeeded),
            "failed_count": len(failed),
            result_key: succeeded,
            "failed": failed,
        }

    def update_metadata_many(self, changes: list[tuple[str, MetadataChange]]) -> dict[str, Any]:
        """Edit titles, summaries, tags, and extracted fields of many visible documents."""
        return self._apply_batch(
            [
                (name, lambda name=name, change=change: summarize(self.update_metadata(name, change), DATA_FIELDS))
                for name, change in changes
            ],
            "updated",
        )

    def rename_many(self, renames: list[tuple[str, str]]) -> dict[str, Any]:
        """Rename many visible documents in order, so chained renames work."""

        def rename(name: str, new_name: str) -> Row:
            renamed = self.rename(name, new_name)
            return {"old_name": name, "name": renamed["name"], "public_url": renamed["public_url"]}

        return self._apply_batch(
            [
                (name, lambda name=name, new_name=new_name: rename(name, new_name))
                for name, new_name in renames
            ],
            "renamed",
        )

    def favorites(self) -> list[Row]:
        return self.documents.list_favorites()

    def stats(self, top_tags: int = 10) -> dict[str, Any]:
        """Summarize the visible library from stored metadata, without opening files."""
        rows = self.list()
        total_bytes = sum(row["size_bytes"] for row in rows)
        formats = Counter(row["file_format"].lower() for row in rows)
        types = Counter(row["doc_type"] for row in rows)
        years = Counter(row["financial_year"] or "unknown" for row in rows)
        tags = Counter(tag for row in rows for tag in row["tags"])
        largest = max(rows, key=lambda row: row["size_bytes"], default=None)
        updated = sorted(str(row["updated_at"]) for row in rows)
        return {
            "total_documents": len(rows),
            "total_pages": sum(row["page_count"] for row in rows),
            "favorite_count": sum(1 for row in rows if row["favorite"]),
            "needs_review_count": sum(1 for row in rows if row["review_flags"]),
            "extraction_failed_count": sum(1 for row in rows if row["extraction_error"]),
            "by_doc_type": dict(types.most_common()),
            "by_financial_year": dict(sorted(years.items(), reverse=True)),
            "total_size_bytes": total_bytes,
            "total_size": self.human_bytes(total_bytes),
            "average_size_bytes": total_bytes // len(rows) if rows else 0,
            "largest_document": (
                {
                    "name": largest["name"],
                    "size_bytes": largest["size_bytes"],
                    "size": self.human_bytes(largest["size_bytes"]),
                }
                if largest
                else None
            ),
            "formats": dict(formats.most_common()),
            "unique_tag_count": len(tags),
            "top_tags": [
                {"tag": tag, "count": count}
                for tag, count in tags.most_common(max(top_tags, 0))
            ],
            "oldest_updated_at": updated[0] if updated else None,
            "newest_updated_at": updated[-1] if updated else None,
        }

    @staticmethod
    def human_bytes(size: int) -> str:
        """Format a byte count with the largest binary unit that keeps it >= 1."""
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if value < 1024:
                return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TiB"

    def list_page(
        self,
        limit: int,
        offset: int,
        max_limit: int,
        doc_types: list[str] | None = None,
        financial_year: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of compact document summaries, newest first."""
        rows = self.list(doc_types, financial_year)
        limit, offset = min(max(limit, 1), max_limit), max(offset, 0)
        return page([summarize(row) for row in rows[offset : offset + limit]], len(rows), limit, offset)

    def set_favorite(self, names: list[str], favorite: bool) -> dict[str, Any]:
        """Add visible documents to Favorites or remove them."""
        clean_names = unique_names(names, "Select at least one document.")
        for name in clean_names:
            self.path(name)
        changed = self.documents.set_favorite(clean_names, favorite)
        return {
            "status": "favorited" if favorite else "unfavorited",
            "names": changed,
            "count": len(changed),
        }

    def public_url(self, name: str) -> str:
        self.path(name)
        return self.get(name)["public_url"]

    def public_path(self, identifier: str) -> Path:
        """Resolve a stable document ID, with filename compatibility."""
        return self.path(self.documents.name_for_document_id(identifier) or identifier)

    def select_paths(self, names: list[str], max_documents: int, max_bytes: int) -> list[Path]:
        """Resolve a bounded, deduplicated batch of visible document paths."""
        clean_names = unique_names(names, "Provide at least one document name.")
        if len(clean_names) > max_documents:
            raise ValueError(f"Select at most {max_documents} documents at a time.")
        paths = [self.path(name) for name in clean_names]
        if sum(path.stat().st_size for path in paths) > max_bytes:
            raise ValueError(
                f"Selected documents exceed the {self.format_bytes(max_bytes)} limit; "
                "request fewer documents."
            )
        return paths

    @staticmethod
    def format_bytes(size: int) -> str:
        if size >= 1024**3 and size % 1024**3 == 0:
            return f"{size // 1024**3} GiB"
        return f"{size / 1024**2:.0f} MiB"


@dataclass(frozen=True)
class PreviewResult:
    """A rendered preview plus the text an LLM needs to describe and link it."""

    jpeg: bytes
    items: list[Row]
    unavailable: list[str] = field(default_factory=list)
    page: int | None = None

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "previewed": [
                {"number": number, **summarize(item, PREVIEW_FIELDS)}
                for number, item in enumerate(self.items, start=1)
            ],
        }
        if self.page is not None:
            result["page"] = self.page
            result["page_count"] = self.items[0]["page_count"]
        if len(self.items) > 1:
            result["note"] = "Numbers match the labels on the sheet of first pages."
        if self.unavailable:
            result["unavailable"] = self.unavailable
            result["unavailable_reason"] = "Not found, renamed, or not visible; re-run search_documents."
        return result


class DocumentPreviewService:
    """Render chat previews of visible documents, skipping names that cannot be shown.

    A stale or hidden name is reported rather than failing the batch; only
    downscaled first pages are sent, rendered one document at a time.
    """

    def __init__(self, visible: VisibleDocumentService, renderer: PreviewRenderer, max_documents: int) -> None:
        self.visible = visible
        self.renderer = renderer
        self.max_documents = max_documents

    def preview(self, names: list[str]) -> PreviewResult:
        clean_names = unique_names(names, "Provide at least one document name.")
        if len(clean_names) > self.max_documents:
            raise ValueError(
                f"Preview at most {self.max_documents} documents at a time; "
                "use delivery='zip' to download more."
            )
        paths, items, unavailable = [], [], []
        for name in clean_names:
            try:
                path, item = self.visible.path(name), self.visible.get(name)
            except (FileNotFoundError, PermissionError, ValueError):
                unavailable.append(name)
                continue
            paths.append(path)
            items.append(item)
        if not paths:
            raise FileNotFoundError(
                f"None of the requested documents are available: {', '.join(unavailable)}. "
                "Re-run search_documents or list_documents for current names."
            )
        return PreviewResult(self.renderer.render(paths), items, unavailable)

    def page(self, name: str, page_number: int = 1) -> PreviewResult:
        """Render one readable page of one visible document."""
        path, item = self.visible.path(name), self.visible.get(name)
        return PreviewResult(self.renderer.render_page(path, page_number), [item], page=page_number)


class DocumentDownloadService:
    """Package visible documents as a ZIP behind a short-lived public download link.

    Archives are never embedded in tool results: MCP clients reject large
    messages, and chat apps cannot hand an embedded blob to the user as a file.
    """

    DOWNLOAD_PATH = "/api/downloads/"

    def __init__(
        self,
        visible: VisibleDocumentService,
        store: DownloadStore,
        urls: PublicURLService,
        max_documents: int,
        max_bytes: int,
    ) -> None:
        self.visible = visible
        self.store = store
        self.urls = urls
        self.max_documents = max_documents
        self.max_bytes = max_bytes

    def create_zip(self, names: list[str]) -> dict[str, Any]:
        """Archive the named visible documents and return their download link."""
        base_url = self.urls.base_url()
        paths = self.visible.select_paths(names, self.max_documents, self.max_bytes)
        ticket = self.store.create(paths)
        return {
            "status": "zip_ready",
            "download_url": f"{base_url}{self.DOWNLOAD_PATH}{ticket.token}",
            "file_name": ticket.file_name,
            "document_count": ticket.document_count,
            "size_bytes": ticket.size_bytes,
            "expires_at": ticket.expires_at_iso,
            "names": [path.name for path in paths],
        }

    def archive(self, token: str) -> tuple[DownloadTicket, Path]:
        """Return the live ticket and archive file for a download token."""
        return self.store.resolve(token), self.store.archive_path(token)


class UploadLinkService:
    """Let HTTP-capable clients upload file bytes instead of Base64 tool arguments.

    LLMs cannot emit megabytes of Base64, but agents with a shell or code
    sandbox can POST a local file to a single-use link.
    """

    UPLOAD_PATH = "/api/uploads/"

    def __init__(
        self,
        visible: VisibleDocumentService,
        store: UploadLinkStore,
        urls: PublicURLService,
        max_bytes: int,
    ) -> None:
        self.visible = visible
        self.store = store
        self.urls = urls
        self.max_bytes = max_bytes

    def create_link(self, name: str, replace: bool = False) -> dict[str, Any]:
        if replace:
            self.visible.path(name)
        ticket = self.store.create(name, replace)
        url = f"{self.urls.base_url()}{self.UPLOAD_PATH}{ticket.token}"
        return {
            "status": "upload_link_ready",
            "upload_url": url,
            "method": "POST",
            "name": name,
            "replace": replace,
            "max_bytes": self.max_bytes,
            "expires_at": ticket.expires_at_iso,
            "usage": (
                "POST the raw file bytes or a multipart form field named 'file'. "
                "For a password-protected PDF add a form field or query parameter "
                "named 'password'. The link works once."
            ),
            "curl_example": f"curl -fsS -F 'file=@/path/to/document.pdf' '{url}'",
        }

    def receive(
        self, token: str, data: bytes, file_name: str | None = None, password: str | None = None
    ) -> dict[str, Any]:
        """Store the uploaded bytes for a claimed ticket; failed attempts can retry."""
        ticket = self.store.claim(token)
        try:
            if ticket.replace:
                return {
                    "status": "replaced",
                    **self.visible.ingest_bytes(ticket.name, data, replace=True, password=password),
                }
            return self.visible.upload(ticket.name or file_name, data, password)
        except BaseException:
            self.store.release(ticket)
            raise


class HiddenDocumentService(DocumentCollectionService):
    """The authenticated Hidden Vault: list, edit, delete, hide, and restore."""

    HIDDEN = True
    WRONG_COLLECTION_MESSAGE = "Only hidden documents can use this route."

    def set_hidden(self, names: list[str], hidden: bool) -> dict[str, Any]:
        """Move documents into or out of the logical Hidden Vault."""
        clean_names = unique_names(names, "Select at least one document.")
        for name in clean_names:
            self.storage.existing_path(name)
        changed = self.documents.set_hidden(clean_names, hidden)
        return {
            "status": "hidden" if hidden else "restored",
            "names": changed,
            "count": len(changed),
        }

    def remove_many(self, names: list[str]) -> dict[str, Any]:
        clean_names = unique_names(names, "Select at least one hidden document.")
        for name in clean_names:
            self.remove(name)
        return {"status": "deleted", "count": len(clean_names)}


class SearchService:
    """Search visible metadata and page text with normalized words, tags, and filters."""

    def __init__(self, documents: DocumentRepository, rules: MetadataRules, limit_max: int) -> None:
        self.documents = documents
        self.rules = rules
        self.limit_max = limit_max

    def search(
        self,
        query: str,
        tags: list[str] | None,
        match_all_tags: bool,
        limit: int,
        offset: int,
        doc_types: list[str] | None = None,
        financial_year: str | None = None,
    ) -> dict[str, Any]:
        normalized_query = self.rules.search_query(query)
        normalized_tags = self.rules.normalize_tags(tags) if tags else []
        selection = DocumentFilter.create(doc_types, financial_year)
        if not normalized_query and not normalized_tags and not doc_types and not financial_year:
            raise ValueError("Provide a meaningful query, a tag, a doc_type, or a financial_year.")
        limit, offset = min(max(limit, 1), self.limit_max), max(offset, 0)
        count, results = self.documents.search(
            normalized_query,
            normalized_tags,
            match_all_tags,
            selection.doc_types,
            selection.financial_year,
            limit,
            offset,
        )
        next_offset = offset + len(results)
        return {
            "query": query,
            "normalized_query": normalized_query,
            "count": count,
            "next_offset": next_offset if next_offset < count else None,
            "results": [summarize(row) for row in results],
        }


class LexiaService:
    """Facade grouping every Luna Lexia use case for the transport adapters."""

    def __init__(
        self,
        visible: VisibleDocumentService,
        hidden: HiddenDocumentService,
        search: SearchService,
        indexer: DocumentIndexer,
        urls: PublicURLService,
        downloads: DocumentDownloadService,
        uploads: UploadLinkService,
        previews: DocumentPreviewService,
        max_read_chars: int,
    ) -> None:
        self.visible = visible
        self.hidden = hidden
        self.search = search
        self.indexer = indexer
        self.urls = urls
        self.downloads = downloads
        self.uploads = uploads
        self.previews = previews
        self.max_read_chars = max_read_chars

    @staticmethod
    def document_types() -> list[dict[str, Any]]:
        """Describe every document type and the fields extracted for it."""
        return [kind.describe() for kind in DOCUMENT_TYPES.values()]

    @classmethod
    def create(
        cls,
        config: AppConfig,
        documents: DocumentRepository,
        public_urls: PublicURLRepository,
        parser: DoclingParser,
        analyzer: DocumentAnalyzer,
    ) -> LexiaService:
        """Wire the use cases around the given persistence and analysis adapters."""
        rules = MetadataRules(config.max_tags, config.search_stop_words)
        reader = analyzer.reader
        storage = DocumentStorage(config.document_dir, set(config.allowed_formats))
        urls = PublicURLService(public_urls)
        urls.seed(config.public_base_url)
        indexer = DocumentIndexer(storage, documents, parser, analyzer, urls, rules)
        visible = VisibleDocumentService(
            storage,
            documents,
            rules,
            indexer,
            DocumentPayloadDecoder(config.allowed_formats, config.max_document_bytes),
            config.max_fetch_documents,
        )
        return cls(
            visible=visible,
            hidden=HiddenDocumentService(storage, documents, rules, indexer),
            search=SearchService(documents, rules, config.search_limit_max),
            indexer=indexer,
            urls=urls,
            downloads=DocumentDownloadService(
                visible,
                DownloadStore(
                    config.download_dir,
                    config.download_ttl_seconds,
                    config.max_download_store_bytes,
                ),
                urls,
                config.max_archive_documents,
                config.max_archive_bytes,
            ),
            uploads=UploadLinkService(
                visible, UploadLinkStore(config.upload_ttl_seconds), urls, config.max_document_bytes
            ),
            previews=DocumentPreviewService(
                visible, PreviewRenderer(reader, config.display_max_edge), config.max_fetch_documents
            ),
            max_read_chars=config.max_read_chars,
        )
