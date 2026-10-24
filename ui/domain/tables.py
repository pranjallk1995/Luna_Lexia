"""Turn extracted values into editable table rows and edited rows back into values.

The details tables show every value as text; the backend parses and validates
whatever is typed, with the same rules it applies to model output.
"""

from typing import Any

VALUE_COLUMN = "Value"


def cell(value: Any) -> str:
    """Render one extracted value as editable text."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def table_rows(values: list | None, columns: list[str] | None) -> list[dict[str, str]]:
    """Rows for a list of objects (one column per field) or of plain values (one column)."""
    if columns is None:
        return [{VALUE_COLUMN: cell(value)} for value in values or []]
    return [{column: cell((item or {}).get(column)) for column in columns} for item in values or []]


def apply_edits(rows: list[dict], state: dict) -> list[dict]:
    """Rebuild a table from st.data_editor's edited, deleted, and added rows.

    Edited and deleted row numbers refer to the original rows, so edits are
    applied before deletions; added rows go at the end.
    """
    updated = [dict(row) for row in rows]
    for index, change in (state.get("edited_rows") or {}).items():
        updated[int(index)].update(change)
    deleted = {int(index) for index in state.get("deleted_rows") or []}
    updated = [row for index, row in enumerate(updated) if index not in deleted]
    updated.extend(dict(row) for row in state.get("added_rows") or [])
    return updated


def _clean(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def table_values(rows: list[dict], columns: list[str] | None) -> list:
    """The list to save: blank cells become null and fully blank rows are dropped."""
    if columns is None:
        return [text for row in rows if (text := _clean(row.get(VALUE_COLUMN)))]
    cleaned = [{column: _clean(row.get(column)) for column in columns} for row in rows]
    return [row for row in cleaned if any(value is not None for value in row.values())]
