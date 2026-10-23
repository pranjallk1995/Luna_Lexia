"""Shared Streamlit building blocks for the Luna Lexia tabs."""

import html
from dataclasses import dataclass

import streamlit as st

from config import UIConfig
from services.api import LunaLexiaAPI
from services.documents import DocumentCollection, DocumentFiles
from services.state import SessionState


class ResponsiveLayout:
    """Adapt gallery density and copy to phone, tablet, or desktop browsers."""

    MOBILE_TOKENS = ("android", "iphone", "ipad", "ipod", "mobile")

    def __init__(self, config: UIConfig) -> None:
        self.config = config

    @classmethod
    def is_mobile(cls) -> bool:
        user_agent = st.context.headers.get("User-Agent", "").casefold()
        return any(token in user_agent for token in cls.MOBILE_TOKENS)

    def gallery_columns(self) -> int:
        """Use one column in list view or on mobile, otherwise the configured count."""
        if SessionState.is_list_view() or self.is_mobile():
            return 1
        return self.config.gallery_columns

    def search_placeholder(self) -> str:
        return "Name, type, or tag" if self.is_mobile() else "Search by filename, type, year, issuer, or tag"


class DocumentTypes:
    """Labels and field schemas of every document type, fetched once per session."""

    STATE_KEY = "document_types"

    def __init__(self, api: LunaLexiaAPI) -> None:
        if self.STATE_KEY not in st.session_state:
            st.session_state[self.STATE_KEY] = {item["doc_type"]: item for item in api.document_types()}
        self.types: dict[str, dict] = st.session_state[self.STATE_KEY]

    def keys(self) -> list[str]:
        return list(self.types)

    def label(self, doc_type: str | None) -> str:
        return self.types.get(doc_type or "", {}).get("label", "Document")

    def fields(self, doc_type: str) -> dict[str, dict]:
        """Return the type's top-level field schemas in their declared order."""
        return self.types.get(doc_type, {}).get("fields", {}).get("properties", {})


@dataclass(frozen=True)
class LexiaContext:
    """Dependencies every tab, gallery, and dialog needs."""

    api: LunaLexiaAPI
    config: UIConfig
    files: DocumentFiles
    layout: ResponsiveLayout

    @classmethod
    def create(cls, config: UIConfig) -> "LexiaContext":
        return cls(LunaLexiaAPI(config), config, DocumentFiles(config), ResponsiveLayout(config))

    @property
    def types(self) -> DocumentTypes:
        return DocumentTypes(self.api)


class DocumentLabel:
    """A single-line, truncated filename with its full value as a tooltip."""

    @staticmethod
    def render(container, stored_name: str) -> None:
        display_name = html.escape(DocumentFiles.display_name(stored_name), quote=True)
        container.markdown(
            f"""<div title="{display_name}" style="
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                color: var(--lv-muted);
                font-size: 0.875rem;
                line-height: 1.25rem;
                margin-bottom: 0.25rem;
            ">{display_name}</div>""",
            unsafe_allow_html=True,
        )


class SearchFilter:
    """A search box that narrows a document collection by name, type, year, or tag."""

    def __init__(
        self, layout: ResponsiveLayout, key: str, label: str, no_match_message: str
    ) -> None:
        self.layout = layout
        self.key = key
        self.label = label
        self.no_match_message = no_match_message

    def apply(self, collection: DocumentCollection) -> DocumentCollection | None:
        """Return the matches, or None after reporting that a query matched nothing."""
        query = (
            st.text_input(
                self.label,
                placeholder=self.layout.search_placeholder(),
                key=self.key,
                icon=":material/search:",
                label_visibility="collapsed",
            )
            .strip()
            .casefold()
        )
        filtered = collection.matching(query)
        if query and not filtered.documents:
            st.info(self.no_match_message)
            return None
        return filtered


class LexiaTab:
    """Base class giving every tab its shared dependencies."""

    def __init__(self, context: LexiaContext) -> None:
        self.context = context
        self.api = context.api
        self.files = context.files
        self.layout = context.layout

    def render(self) -> None:
        raise NotImplementedError
