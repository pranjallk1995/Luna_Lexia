"""The document analysis workflow as a LangGraph state graph.

    parse ─▶ classify ─┬─▶ extract_text ──┬────────────────────▶ validate ─▶ tag
                       │                  └─▶ extract_images ─▶─┘
                       └─▶ extract_images (no readable text) ──▶─┘

Docling parses the file, the classification agent picks its type, and the
type's extraction agent fills the schema from the parsed text. When that fails,
or no text could be read, extraction is tried once more from page images.
Validation then turns business-rule failures into review flags, and tagging adds
the type, financial-year, and needs-review tags.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.models import DocumentAnalysis, DocumentClassification, DocumentContent, MetadataRules
from app.core.schemas import DOCUMENT_TYPES, DocumentType, ExtractedDetails, KeyFacts, document_type
from app.integrations.extraction import DocumentAnalyzer
from app.integrations.parsing import DoclingParser


class PipelineState(TypedDict, total=False):
    """What the graph knows about one document as it moves through the nodes."""

    data: bytes
    hint: DocumentType | None
    content: DocumentContent
    classification: DocumentClassification
    kind: DocumentType
    details: ExtractedDetails | None
    from_images: bool
    errors: list[str]
    analysis: DocumentAnalysis


class DocumentPipeline:
    """Run parsing, classification, extraction, and validation as one compiled graph."""

    LOW_CONFIDENCE = 0.6
    NO_TEXT_FLAG = "No text could be read; the details were taken from page images."
    IMAGE_FALLBACK_FLAG = "Extraction from the text failed; the details were read from page images."
    FAILED_FLAG = "Details could not be extracted; re-extract the document or enter them manually."

    def __init__(self, parser: DoclingParser, analyzer: DocumentAnalyzer, rules: MetadataRules) -> None:
        self.parser = parser
        self.analyzer = analyzer
        self.rules = rules
        graph = StateGraph(PipelineState)
        graph.add_node("parse", self.parse)
        graph.add_node("classify", self.classify)
        graph.add_node("extract_text", self.extract_text)
        graph.add_node("extract_images", self.extract_images)
        graph.add_node("validate", self.validate)
        graph.add_node("tag", self.tag)
        graph.add_edge(START, "parse")
        graph.add_edge("parse", "classify")
        graph.add_conditional_edges("classify", self.first_extraction, ["extract_text", "extract_images"])
        graph.add_conditional_edges("extract_text", self.after_text, ["validate", "extract_images"])
        graph.add_edge("extract_images", "validate")
        graph.add_edge("validate", "tag")
        graph.add_edge("tag", END)
        self.graph = graph.compile(name="lexia-document-pipeline")

    def run(self, data: bytes, doc_type: str | None = None) -> tuple[DocumentAnalysis, DocumentContent]:
        """Analyze document bytes; ``doc_type`` overrides the classification when known."""
        hint = document_type(doc_type) if doc_type else None
        state = self.graph.invoke({"data": data, "hint": hint, "errors": [], "from_images": False})
        return state["analysis"], state["content"]

    # Nodes

    def parse(self, state: PipelineState) -> PipelineState:
        data = state["data"]
        return {"content": self.parser.parse(data, self.analyzer.reader.file_format(data))}

    def classify(self, state: PipelineState) -> PipelineState:
        classification = self.analyzer.classify(state["data"], state["content"], state["hint"])
        return {
            "classification": classification,
            "kind": state["hint"] or document_type(classification.doc_type),
        }

    def _extract(self, state: PipelineState, from_images: bool) -> PipelineState:
        try:
            details = self.analyzer.extract(state["data"], state["content"], state["kind"], from_images)
        except ValueError as failure:
            return {"details": None, "from_images": from_images, "errors": [*state["errors"], str(failure)]}
        return {"details": details, "from_images": from_images}

    def extract_text(self, state: PipelineState) -> PipelineState:
        return self._extract(state, from_images=False)

    def extract_images(self, state: PipelineState) -> PipelineState:
        return self._extract(state, from_images=True)

    def validate(self, state: PipelineState) -> PipelineState:
        """Turn model confidence, fallbacks, and business rules into review flags."""
        classification, kind, details = state["classification"], state["kind"], state.get("details")
        flags = []
        hint = state["hint"]
        if hint and classification.doc_type != hint.key:
            flags.append(
                f"Stored as a {hint.label}, but it reads like a "
                f"{DOCUMENT_TYPES[classification.doc_type].label}."
            )
        elif classification.confidence < self.LOW_CONFIDENCE:
            flags.append(f"The document type was identified with low confidence ({classification.confidence:.0%}).")
        if state["from_images"]:
            flags.append(self.IMAGE_FALLBACK_FLAG if state["content"].has_text else self.NO_TEXT_FLAG)
        if details is None:
            flags.append(self.FAILED_FLAG)
        else:
            flags.extend(details.review_flags())
        return {
            "analysis": DocumentAnalysis(
                doc_type=kind.key,
                title=self.rules.title(classification.title[: self.rules.TITLE_MAX_LENGTH]),
                summary=self.rules.summary(classification.summary[: self.rules.SUMMARY_MAX_LENGTH]),
                tags=self.rules.normalize_tags(classification.tags),
                confidence=classification.confidence,
                extracted=details.model_dump(mode="json") if details else {},
                facts=details.key_facts() if details else KeyFacts(),
                review_flags=flags,
                extraction_error="; ".join(state["errors"])[:1000] if details is None else None,
            )
        }

    def tag(self, state: PipelineState) -> PipelineState:
        """Add the type and financial-year tags, and needs-review when anything is flagged."""
        analysis = state["analysis"]
        system = [DOCUMENT_TYPES[analysis.doc_type].tag] if DOCUMENT_TYPES[analysis.doc_type].tag else []
        if analysis.facts.financial_year:
            system.append(f"fy {analysis.facts.financial_year}")
        tags = self.rules.with_review_tag(self.rules.with_tags(analysis.tags, system), analysis.review_flags)
        return {"analysis": replace(analysis, tags=tags)}

    # Routes

    @staticmethod
    def first_extraction(state: PipelineState) -> str:
        return "extract_text" if state["content"].has_text else "extract_images"

    @staticmethod
    def after_text(state: PipelineState) -> str:
        return "validate" if state.get("details") is not None else "extract_images"
