/** Extracted values as editable table rows, and edited rows back into values.
 *
 * The details tables show every value as text; the backend parses and validates
 * whatever is typed, with the same rules it applies to model output.
 */

import type { DocumentType, FieldSchema } from "./api";

export const VALUE_COLUMN = "Value";

export type Row = Record<string, string>;

/** One extracted value as editable text. */
export const cell = (value: unknown): string => (value === null || value === undefined ? "" : String(value));

/** Rows for a list of objects (one column per field) or of plain values (one column). */
export function tableRows(values: unknown, columns: string[] | null): Row[] {
  const items = Array.isArray(values) ? values : [];
  if (!columns) return items.map((value) => ({ [VALUE_COLUMN]: cell(value) }));
  return items.map((item) =>
    Object.fromEntries(columns.map((column) => [column, cell((item as Record<string, unknown> | null)?.[column])])),
  );
}

const clean = (value?: string | null) => (value ?? "").trim() || null;

/** The list to save: blank cells become null and fully blank rows are dropped. */
export function tableValues(rows: Row[], columns: string[] | null): unknown[] {
  if (!columns) return rows.flatMap((row) => clean(row[VALUE_COLUMN]) ?? []);
  return rows
    .map((row) => Object.fromEntries(columns.map((column) => [column, clean(row[column])])))
    .filter((row) => Object.values(row).some((value) => value !== null));
}

export const fieldLabel = (field: string) => {
  const words = field.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
};

export const isListField = (schema: FieldSchema) =>
  schema.type === "array" || Boolean(schema.anyOf?.some((option) => option.type === "array"));

/** Columns of a list field's rows, or null when its items are plain values. */
export function itemColumns(type: DocumentType | undefined, field: string): string[] | null {
  const fields = type?.fields;
  const schema = fields?.properties?.[field];
  if (!schema) return null;
  const options = [schema, ...(schema.anyOf ?? [])];
  let items = options.find((option) => option.type === "array")?.items;
  if (items?.$ref) items = fields?.$defs?.[items.$ref.split("/").pop()!] as typeof items;
  return items?.properties ? Object.keys(items.properties) : null;
}
