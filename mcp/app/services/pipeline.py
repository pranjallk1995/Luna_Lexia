"""The document analysis workflow: parse, classify, extract, validate, and tag.

Docling parses the file, the classification agent picks its type, and the
type's extraction agent fills the schema from the parsed text. When that fails,
or no text could be read, extraction is tried once more from page images.
Validation then turns business-rule failures into review flags, and tagging adds
the type, financial-year, and needs-review tags.
"""

from __future__ import annotations

from dataclasses import replace

from app.core.models import DocumentAnalysis, DocumentClassification, DocumentContent, MetadataRules
from app.core.schemas import DOCUMENT_TYPES, DocumentType, ExtractedDetails, KeyFacts, document_type
from app.integrations.extraction import DocumentAnalyzer
from app.integrations.parsing import DoclingParser


class DocumentPipeline:
    """Run parsing, classification, extraction (with a page-image fallback), and validation."""

    LOW_CONFIDENCE = 0.6
    NO_TEXT_FLAG = "No text could be read; the details were taken from page images."
    IMAGE_FALLBACK_FLAG = "Extraction from the text failed; the details were read from page images."
    FAILED_FLAG = "Details could not be extracted; re-extract the document or enter them manually."

    def __init__(self, parser: DoclingParser, analyzer: DocumentAnalyzer, rules: MetadataRules) -> None:
        self.parser = parser
        self.analyzer = analyzer
        self.rules = rules

    def run(self, data: bytes, doc_type: str | None = None) -> tuple[DocumentAnalysis, DocumentContent]:
        """Analyze document bytes; ``doc_type`` overrides the classification when known."""
        hint = document_type(doc_type) if doc_type else None
        content = self.parser.parse(data, self.analyzer.reader.file_format(data))
        classification = self.analyzer.classify(data, content, hint)
        kind = hint or document_type(classification.doc_type)
        errors: list[str] = []
        from_images = not content.has_text
        details = self._extract(data, content, kind, from_images, errors)
        if details is None and not from_images:
            from_images = True
            details = self._extract(data, content, kind, from_images, errors)
        analysis = self.validate(content, classification, kind, hint, details, from_images, errors)
        return self.tag(analysis), content

    def _extract(
        self, data: bytes, content: DocumentContent, kind: DocumentType, from_images: bool, errors: list[str]
    ) -> ExtractedDetails | None:
        try:
            return self.analyzer.extract(data, content, kind, from_images)
        except ValueError as failure:
            errors.append(str(failure))
            return None

    def validate(
        self,
        content: DocumentContent,
        classification: DocumentClassification,
        kind: DocumentType,
        hint: DocumentType | None,
        details: ExtractedDetails | None,
        from_images: bool,
        errors: list[str],
    ) -> DocumentAnalysis:
        """Turn model confidence, fallbacks, and business rules into review flags."""
        flags = []
        if hint and classification.doc_type != hint.key:
            flags.append(
                f"Stored as a {hint.label}, but it reads like a "
                f"{DOCUMENT_TYPES[classification.doc_type].label}."
            )
        elif classification.confidence < self.LOW_CONFIDENCE:
            flags.append(f"The document type was identified with low confidence ({classification.confidence:.0%}).")
        if from_images:
            flags.append(self.IMAGE_FALLBACK_FLAG if content.has_text else self.NO_TEXT_FLAG)
        if details is None:
            flags.append(self.FAILED_FLAG)
        else:
            flags.extend(details.review_flags())
        return DocumentAnalysis(
            doc_type=kind.key,
            title=self.rules.title(classification.title[: self.rules.TITLE_MAX_LENGTH]),
            summary=self.rules.summary(classification.summary[: self.rules.SUMMARY_MAX_LENGTH]),
            tags=self.rules.normalize_tags(classification.tags),
            confidence=classification.confidence,
            extracted=details.model_dump(mode="json") if details else {},
            facts=details.key_facts() if details else KeyFacts(),
            review_flags=flags,
            extraction_error="; ".join(errors)[:1000] if details is None else None,
        )

    def tag(self, analysis: DocumentAnalysis) -> DocumentAnalysis:
        """Add the type and financial-year tags, and needs-review when anything is flagged."""
        system = [DOCUMENT_TYPES[analysis.doc_type].tag] if DOCUMENT_TYPES[analysis.doc_type].tag else []
        if analysis.facts.financial_year:
            system.append(f"fy {analysis.facts.financial_year}")
        tags = self.rules.with_review_tag(self.rules.with_tags(analysis.tags, system), analysis.review_flags)
        return replace(analysis, tags=tags)
