"""Document dialog: page viewer, inline name/title/summary/tag editing, and extracted details."""

import html
from dataclasses import dataclass
from pathlib import Path

import httpx
import pandas as pd
import streamlit as st

from config import UIConfig
from domain.tables import VALUE_COLUMN, apply_edits, cell, table_rows, table_values
from services.api import LunaLexiaAPI
from views.actions import GalleryActions
from views.shared import LexiaContext


@dataclass(frozen=True)
class PreviewKeys:
    """Session-state and widget keys for one document's preview dialog."""

    name: str

    def editing(self, field: str) -> str:
        return f"preview_{field}_edit_{self.name}"

    def draft(self, field: str) -> str:
        return f"{self.editing(field)}_draft"

    def error(self, field: str) -> str:
        return f"{self.editing(field)}_error"

    @property
    def reload(self) -> str:
        """Set after a change that moves the file or reshapes its card, to rerun the app."""
        return f"preview_reload_{self.name}"

    @property
    def page(self) -> str:
        return f"preview_page_{self.name}"

    @property
    def tag_adding(self) -> str:
        return f"preview_tag_add_{self.name}"

    @property
    def tag_draft(self) -> str:
        return f"{self.tag_adding}_draft"

    @property
    def tag_error(self) -> str:
        return f"{self.tag_adding}_error"

    @property
    def generation(self) -> str:
        return f"preview_generation_{self.name}"

    @property
    def pills(self) -> str:
        """Tag pills key; its generation suffix remounts the pills after edits."""
        return f"preview_tag_pills_{self.name}_{st.session_state[self.generation]}"

    @property
    def details_editor(self) -> str:
        return f"preview_details_{self.name}_{st.session_state[self.generation]}"

    def list_editor(self, field: str) -> str:
        return f"preview_list_{field}_{self.name}_{st.session_state[self.generation]}"

    @property
    def details_error(self) -> str:
        return f"preview_details_error_{self.name}"

    @property
    def reextract_type(self) -> str:
        return f"preview_reextract_type_{self.name}"


class DocumentPreviewDialog:
    """Show a document's pages beside its metadata and extracted details.

    Title, summary, tag, and detail edits update ``metadata_by_name`` so the
    dialog's own fragment reruns show the saved values without reloading the
    page. A rename moves the file and a re-extraction reshapes the card, so
    both close the dialog and rerun the whole app instead.
    """

    TEXT_FIELDS = {
        "title": ("Title", 120, ":material/title:"),
        "summary": ("Summary", 1000, ":material/notes:"),
    }

    def __init__(
        self, context: LexiaContext, actions: GalleryActions, metadata_by_name: dict[str, dict]
    ) -> None:
        self.files = context.files
        self.types = context.types
        self.layout = context.layout
        self.actions = actions
        self.metadata_by_name = metadata_by_name

    def open(self, document_path: Path) -> None:
        # Closing with the X reruns the app, so the server knows the dialog closed and the
        # next thumbnail opens on its first click instead of needing a second one.
        @st.dialog(self.files.display_name(document_path.name), width="large", on_dismiss="rerun")
        def show_document() -> None:
            self._render(document_path)

        show_document()

    @staticmethod
    def _marker(container, css_class: str) -> None:
        container.markdown(f'<span class="{css_class}"></span>', unsafe_allow_html=True)

    def _save(self, name: str, **change) -> None:
        self.metadata_by_name[name] = self.actions.update_metadata(name, **change)

    def _render(self, document_path: Path) -> None:
        keys = PreviewKeys(document_path.name)
        if st.session_state.pop(keys.reload, None):
            st.rerun()
        st.session_state.setdefault(keys.generation, 0)
        metadata = self.metadata_by_name.get(document_path.name) or {}
        # Phones get one column: the page preview first, then the name, title, summary, and tags.
        if self.layout.is_mobile():
            page_column, detail_column = st.container(), st.container()
        else:
            page_column, detail_column = st.columns([5, 6], gap="medium")
        with page_column:
            self._marker(st, "lv-preview-pages")
            self._render_pages(keys, document_path, metadata)
        with detail_column:
            if self.actions.allows_renaming:
                self._render_name(keys, document_path)
            for field in self.TEXT_FIELDS:
                self._render_text(keys, field, str(metadata.get(field) or ""))
            self._render_tags(keys, list(metadata.get("tags") or []))
        self._render_review(keys, metadata)
        self._render_details(keys, metadata)
        self._render_reextract(keys, metadata)

    # Pages

    def _render_pages(self, keys: PreviewKeys, document_path: Path, metadata: dict) -> None:
        page_count = int(metadata.get("page_count") or 1)
        page = 1
        if page_count > 1:
            picker_column, count_column = st.columns([4, 1], vertical_alignment="center")
            page = picker_column.number_input(
                "Page", min_value=1, max_value=page_count, step=1, key=keys.page, label_visibility="collapsed"
            )
            count_column.markdown(f'<div class="lv-card-facts">of {page_count}</div>', unsafe_allow_html=True)
        st.image(self.files.page(document_path, int(page) - 1), width="stretch")

    # Name

    def _save_name(self, keys: PreviewKeys, suffix: str) -> None:
        """Rename the document from Enter or the checkmark, keeping its extension."""
        stem = st.session_state.get(keys.draft("name"), "").strip()
        new_name = f"{stem}{suffix}"
        if new_name == keys.name or stem == Path(self.files.display_name(keys.name)).stem:
            st.session_state[keys.editing("name")] = False
            st.session_state[keys.error("name")] = None
            return
        try:
            self.actions.rename(keys.name, new_name)
        except httpx.HTTPError as error:
            st.session_state[keys.error("name")] = LunaLexiaAPI.error_message(
                error, "Could not rename the document."
            )
            return
        st.session_state[keys.editing("name")] = False
        st.session_state[keys.error("name")] = None
        st.session_state[keys.reload] = True
        st.toast(f"Renamed to {new_name}.", icon=":material/check:")

    def _render_name(self, keys: PreviewKeys, document_path: Path) -> None:
        display_name = self.files.display_name(document_path.name)
        box = st.container()
        self._marker(box, "lv-preview-details")
        icon_column, name_column, edit_column = box.columns([1, 12, 1.4], vertical_alignment="center")
        self._marker(icon_column, "lv-preview-static-icon")
        icon_column.button(":material/description:", disabled=True, key=f"name_icon_{keys.name}", width="stretch")
        if st.session_state.get(keys.editing("name"), False):
            name_column.text_input(
                "Name",
                key=keys.draft("name"),
                max_chars=200,
                label_visibility="collapsed",
                placeholder=f"Letters, numbers, and underscores ({document_path.suffix} is kept)",
                on_change=self._save_name,
                args=(keys, document_path.suffix),
            )
            edit_column.button(
                ":material/check:",
                key=f"save_{keys.editing('name')}",
                width="stretch",
                on_click=self._save_name,
                args=(keys, document_path.suffix),
            )
            if st.session_state.get(keys.error("name")):
                box.error(st.session_state[keys.error("name")])
            return
        name_column.markdown(
            f'<div class="lv-preview-caption">{html.escape(display_name)}</div>',
            unsafe_allow_html=True,
        )
        if edit_column.button(":material/edit:", key=f"open_{keys.editing('name')}", width="stretch"):
            st.session_state[keys.editing("name")] = True
            st.session_state[keys.draft("name")] = Path(display_name).stem
            st.rerun(scope="fragment")

    # Title and summary

    def _save_text(self, keys: PreviewKeys, field: str) -> None:
        """Save an inline title or summary field from Enter or the checkmark."""
        try:
            self._save(keys.name, **{field: st.session_state.get(keys.draft(field), "")})
            st.session_state[keys.editing(field)] = False
            st.session_state[keys.error(field)] = None
        except httpx.HTTPError as error:
            st.session_state[keys.error(field)] = LunaLexiaAPI.error_message(
                error, f"Could not update the {field}."
            )

    def _render_text(self, keys: PreviewKeys, field: str, value: str) -> None:
        label, max_chars, icon = self.TEXT_FIELDS[field]
        box = st.container()
        self._marker(box, "lv-preview-details")
        icon_column, text_column, edit_column = box.columns([1, 12, 1.4], vertical_alignment="center")
        self._marker(icon_column, "lv-preview-static-icon")
        icon_column.button(icon, disabled=True, key=f"{field}_icon_{keys.name}", width="stretch")
        if st.session_state.get(keys.editing(field), False):
            text_column.text_input(
                label,
                key=keys.draft(field),
                max_chars=max_chars,
                label_visibility="collapsed",
                on_change=self._save_text,
                args=(keys, field),
            )
            edit_column.button(
                ":material/check:",
                key=f"save_{keys.editing(field)}",
                width="stretch",
                on_click=self._save_text,
                args=(keys, field),
            )
            if st.session_state.get(keys.error(field)):
                box.error(st.session_state[keys.error(field)])
            return
        text_column.markdown(
            f'<div class="lv-preview-caption">{html.escape(value)}</div>',
            unsafe_allow_html=True,
        )
        if edit_column.button(":material/edit:", key=f"open_{keys.editing(field)}", width="stretch"):
            st.session_state[keys.editing(field)] = True
            st.session_state[keys.draft(field)] = value
            st.rerun(scope="fragment")

    # Tags

    @staticmethod
    def _normalized(tag: str) -> str:
        return " ".join(tag.casefold().split())

    def _save_tag(self, keys: PreviewKeys, tags: list[str]) -> None:
        """Save the inline tag field from Enter or the checkmark."""
        st.session_state[keys.tag_error] = None
        new_tag = st.session_state.get(keys.tag_draft, "").strip()
        if not new_tag:
            st.toast("Enter a tag name before adding it.", icon=":material/info:")
            return
        if self._normalized(new_tag) in {self._normalized(tag) for tag in tags}:
            st.toast("That tag is already on this document.", icon=":material/info:")
            return
        if len(tags) >= UIConfig.MAX_TAGS:
            st.toast(
                f"Maximum of {UIConfig.MAX_TAGS} tags reached. Remove a tag before adding another.",
                icon=":material/info:",
            )
            return
        try:
            self._save(keys.name, add_tags=[new_tag])
            st.session_state[keys.tag_adding] = False
            st.session_state[keys.generation] += 1
        except httpx.HTTPError:
            st.toast("The tag could not be saved. Please try again.", icon=":material/info:")

    def _remove_deselected_tags(self, keys: PreviewKeys, tags: list[str]) -> None:
        """Remove tags deselected from the interactive tag pills."""
        selected = st.session_state.get(keys.pills) or []
        removed = [tag for tag in tags if tag not in selected]
        if not removed:
            return
        try:
            self._save(keys.name, remove_tags=removed)
            st.session_state[keys.tag_error] = None
        except httpx.HTTPError as error:
            st.session_state[keys.tag_error] = LunaLexiaAPI.error_message(
                error, "Could not remove the tag."
            )
        finally:
            st.session_state[keys.generation] += 1

    def _render_tag_pills(self, column, keys: PreviewKeys, tags: list[str]) -> None:
        if not tags:
            return
        self._marker(column, "lv-preview-removable-tags")
        column.pills(
            "Tags",
            options=tags,
            default=tags,
            selection_mode="multi",
            key=keys.pills,
            on_change=self._remove_deselected_tags,
            args=(keys, tags),
            label_visibility="collapsed",
            width="stretch",
        )

    def _render_tags(self, keys: PreviewKeys, tags: list[str]) -> None:
        box = st.container()
        self._marker(box, "lv-preview-details")
        if st.session_state.get(keys.tag_adding, False):
            tags_column, input_column, action_column = box.columns([8, 5, 1.4], vertical_alignment="center")
            self._render_tag_pills(tags_column, keys, tags)
            input_column.text_input(
                "New tag",
                key=keys.tag_draft,
                placeholder="Tag name",
                label_visibility="collapsed",
                on_change=self._save_tag,
                args=(keys, tags),
            )
            self._marker(action_column, "lv-preview-icon-action")
            action_column.button(
                ":material/check:",
                key=f"save_{keys.tag_adding}",
                width="stretch",
                on_click=self._save_tag,
                args=(keys, tags),
            )
            if st.session_state.get(keys.tag_error):
                box.error(st.session_state[keys.tag_error])
            return

        tags_column, action_column = box.columns([13, 1.4], vertical_alignment="center")
        self._render_tag_pills(tags_column, keys, tags)
        self._marker(action_column, "lv-preview-icon-action")
        if action_column.button(":material/add:", key=f"open_{keys.tag_adding}", width="stretch"):
            st.session_state[keys.tag_adding] = True
            st.session_state[keys.tag_draft] = ""
            st.rerun(scope="fragment")

    # Review flags

    def _mark_reviewed(self, keys: PreviewKeys) -> None:
        try:
            self._save(keys.name, mark_reviewed=True)
            st.session_state[keys.generation] += 1
        except httpx.HTTPError as error:
            st.session_state[keys.details_error] = LunaLexiaAPI.error_message(
                error, "Could not mark the document as reviewed."
            )

    def _render_review(self, keys: PreviewKeys, metadata: dict) -> None:
        flags = metadata.get("review_flags") or []
        if not flags:
            return
        message_column, button_column = st.columns([8, 1], vertical_alignment="center")
        message_column.warning(
            "**Check these details before relying on them:**\n"
            + "\n".join(f"- {flag}" for flag in flags),
            icon=":material/fact_check:",
        )
        button_column.button(
            ":material/done_all:",
            help="Mark reviewed: the details are correct, so clear these flags",
            key=f"review_{keys.name}",
            width="stretch",
            on_click=self._mark_reviewed,
            args=(keys,),
        )

    # Extracted details

    @staticmethod
    def _label(field: str) -> str:
        return field.replace("_", " ").capitalize()

    @staticmethod
    def _is_list(schema: dict) -> bool:
        return schema.get("type") == "array" or any(
            option.get("type") == "array" for option in schema.get("anyOf", [])
        )

    def _save_details(self, keys: PreviewKeys, fields: list[str], original: list[str]) -> None:
        """Send only the fields whose cells changed; the backend parses and validates them."""
        edited = st.session_state.get(keys.details_editor) or {}
        values = list(original)
        for row, change in (edited.get("edited_rows") or {}).items():
            if "Value" in change:
                values[int(row)] = "" if change["Value"] is None else str(change["Value"])
        changed = {
            field: (value.strip() or None)
            for field, value, before in zip(fields, values, original)
            if value != before
        }
        if not changed:
            return
        try:
            self._save(keys.name, extracted=changed)
            st.session_state[keys.details_error] = None
        except httpx.HTTPError as error:
            st.session_state[keys.details_error] = LunaLexiaAPI.error_message(
                error, "Could not save the corrected details."
            )
        finally:
            st.session_state[keys.generation] += 1

    def _save_list(self, keys: PreviewKeys, field: str, columns: list[str] | None, rows: list[dict]) -> None:
        """Save a list field after its rows were edited, added, or deleted."""
        state = st.session_state.get(keys.list_editor(field)) or {}
        values = table_values(apply_edits(rows, state), columns)
        if values == table_values(rows, columns):
            return
        try:
            self._save(keys.name, extracted={field: values})
            st.session_state[keys.details_error] = None
        except httpx.HTTPError as error:
            st.session_state[keys.details_error] = LunaLexiaAPI.error_message(
                error, f"Could not save the corrected {self._label(field).lower()}."
            )
        finally:
            st.session_state[keys.generation] += 1

    def _render_list(self, keys: PreviewKeys, doc_type: str, field: str, values: list | None) -> None:
        """An editable table for one list field; rows can be edited, added, and deleted."""
        columns = self.types.item_columns(doc_type, field)
        names = columns or [VALUE_COLUMN]
        rows = table_rows(values, columns)
        with st.expander(f"{self._label(field)} ({len(rows)})"):
            st.data_editor(
                pd.DataFrame(rows, columns=names),
                key=keys.list_editor(field),
                num_rows="dynamic",
                hide_index=True,
                width="stretch",
                on_change=self._save_list,
                args=(keys, field, columns, rows),
                column_config={
                    name: st.column_config.TextColumn(VALUE_COLUMN if columns is None else self._label(name))
                    for name in names
                },
            )

    def _render_details(self, keys: PreviewKeys, metadata: dict) -> None:
        doc_type = metadata.get("doc_type") or "other"
        extracted = metadata.get("extracted") or {}
        facts = [
            self.types.label(doc_type),
            f"FY {metadata['financial_year']}" if metadata.get("financial_year") else None,
            metadata.get("issuer"),
            f"{metadata['confidence']:.0%} confident" if metadata.get("confidence") is not None else None,
            f"Analyzed by {metadata['analysis_model']}" if metadata.get("analysis_model") else None,
        ]
        st.markdown(
            '<div class="lv-preview-heading">Extracted details</div>'
            f'<div class="lv-card-facts">{"<span>·</span>".join(f"<span>{html.escape(str(fact))}</span>" for fact in facts if fact)}</div>',
            unsafe_allow_html=True,
        )
        if metadata.get("extraction_error"):
            st.error(f"Extraction failed: {metadata['extraction_error']}", icon=":material/error:")
        schemas = self.types.fields(doc_type)
        scalars = [field for field, schema in schemas.items() if not self._is_list(schema)]
        original = [cell(extracted.get(field)) for field in scalars]
        st.data_editor(
            pd.DataFrame(
                {"Field": [self._label(field) for field in scalars], "Value": original}
            ),
            key=keys.details_editor,
            disabled=["Field"],
            hide_index=True,
            width="stretch",
            on_change=self._save_details,
            args=(keys, scalars, original),
            column_config={
                "Field": st.column_config.TextColumn(width="small"),
                "Value": st.column_config.TextColumn(
                    help="Edit a value to correct it; clear it if the document does not show it."
                ),
            },
        )
        if st.session_state.get(keys.details_error):
            st.error(st.session_state[keys.details_error])
        for field, schema in schemas.items():
            if self._is_list(schema):
                self._render_list(keys, doc_type, field, extracted.get(field))

    # Re-extraction

    def _render_reextract(self, keys: PreviewKeys, metadata: dict) -> None:
        current = metadata.get("doc_type") or "other"
        type_column, button_column = st.columns([8, 1], vertical_alignment="center")
        doc_type = type_column.selectbox(
            "Document type",
            label_visibility="collapsed",
            options=self.types.keys(),
            index=self.types.keys().index(current) if current in self.types.keys() else 0,
            format_func=self.types.label,
            key=keys.reextract_type,
            help="Choose another type if this one is wrong, then re-extract the details.",
        )
        if button_column.button(
            ":material/refresh:",
            help="Re-extract the details as the selected document type",
            key=f"reextract_{keys.name}",
            width="stretch",
        ):
            with st.spinner("Re-reading the document with the local model..."):
                try:
                    self.metadata_by_name[keys.name] = self.actions.reextract(keys.name, doc_type)
                except httpx.HTTPError as error:
                    st.error(LunaLexiaAPI.error_message(error, "Could not re-extract the document."))
                    return
            st.session_state[keys.generation] += 1
            st.session_state[keys.reload] = True
            st.rerun(scope="fragment")
