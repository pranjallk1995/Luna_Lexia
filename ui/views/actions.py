"""Per-collection document actions used by the shared gallery."""

from collections.abc import Callable
from dataclasses import dataclass

import streamlit as st

from services.api import LunaLexiaAPI
from services.state import SessionState


@dataclass(frozen=True)
class MenuAction:
    """An extra icon button in a document's overflow menu."""

    icon: str
    key: str
    run: Callable[[str], None]


class GalleryActions:
    """What a gallery may do with its documents; visible documents by default."""

    shows_favorites = True
    allows_hiding = True
    allows_renaming = True

    def __init__(self, api: LunaLexiaAPI) -> None:
        self.api = api

    def menu_actions(self) -> list[MenuAction]:
        """Buttons shown after the shared download and delete actions."""
        return [MenuAction(":material/visibility_off:", "hide", SessionState.request_hide)]

    def favorite_names(self) -> set[str]:
        return {item["name"] for item in self.api.favorites()}

    def set_favorite(self, name: str, favorite: bool) -> None:
        self.api.set_favorites([name], favorite)
        SessionState.refresh_gallery()

    def delete(self, name: str) -> None:
        self.api.delete_document(name)
        SessionState.forget_upload(name)
        self._after_delete()

    def hide(self, name: str) -> None:
        self.api.hide_documents([name])
        st.session_state.pending_view_hide = None
        SessionState.refresh_gallery()

    def rename(self, name: str, new_name: str) -> None:
        self.api.rename_document(name, new_name)
        SessionState.refresh_gallery()

    def update_metadata(self, name: str, **change) -> dict:
        return self.api.update_document_metadata(name, self.api.metadata_change(**change))

    def reextract(self, name: str, doc_type: str | None) -> dict:
        return self.api.reextract_document(name, doc_type)

    @staticmethod
    def _after_delete() -> None:
        st.session_state.pending_view_delete = None
        SessionState.refresh_gallery()


class FavoriteActions(GalleryActions):
    """Favorites expose the same actions as View Documents, without hiding."""

    allows_hiding = False

    def menu_actions(self) -> list[MenuAction]:
        return []


class HiddenActions(GalleryActions):
    """Hidden-document actions for the OAuth-authenticated application user."""

    shows_favorites = False
    allows_hiding = False
    allows_renaming = False

    def menu_actions(self) -> list[MenuAction]:
        return [MenuAction(":material/visibility:", "unhide", self.restore)]

    def favorite_names(self) -> set[str]:
        return set()

    def restore(self, name: str) -> None:
        self.api.restore_hidden_documents([name])
        SessionState.refresh_gallery()

    def delete(self, name: str) -> None:
        self.api.delete_hidden_documents([name])
        self._after_delete()

    def update_metadata(self, name: str, **change) -> dict:
        return self.api.update_hidden_metadata(name, self.api.metadata_change(**change))

    def reextract(self, name: str, doc_type: str | None) -> dict:
        return self.api.reextract_hidden_document(name, doc_type)
