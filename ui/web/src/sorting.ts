/** Orders for the gallery: the library's similarity order by default, or by date, name, size, and more. */

import type { DocumentRecord } from "./api";
import { displayName, remember, remembered } from "./lib";

const SORT_KEY = "lv-lexia-sort";

const text = (value?: string | null) => (value ?? "").trim();
/** A to Z, or Z to A with ``direction`` -1; either way blanks go last. */
const byText = (a: string, b: string, direction = 1) => {
  if (!a !== !b) return a ? -1 : 1;
  return direction * a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
};
/** Low to high, or high to low with ``direction`` -1; either way unknowns go last. */
const byNumber = (a?: number | null, b?: number | null, direction = 1) => {
  if ((a == null) !== (b == null)) return a == null ? 1 : -1;
  return direction * ((a ?? 0) - (b ?? 0));
};
const time = (value?: string | null) => (value ? Date.parse(value) : NaN);
/** Earliest first, or latest first with ``direction`` -1; either way undated items go last. */
const byTime = (a?: string | null, b?: string | null, direction = 1) => {
  const [x, y] = [time(a), time(b)];
  if (Number.isNaN(x) !== Number.isNaN(y)) return Number.isNaN(x) ? 1 : -1;
  return direction * ((Number.isNaN(x) ? 0 : x) - (Number.isNaN(y) ? 0 : y));
};

type Option<T> = { id: string; label: string; compare?: (a: T, b: T) => number };

/** Items in the chosen order; similarity keeps the order the library gives. Ties keep their similarity order. */
export function sortItems<T>(items: T[], options: readonly Option<T>[], id: string): T[] {
  const compare = options.find((option) => option.id === id)?.compare;
  return compare ? [...items].sort(compare) : items;
}

export const sortIds = <T,>(options: readonly Option<T>[]) => options.map((option) => option.id);
export const rememberedSort = <T,>(options: readonly Option<T>[]) => remembered(SORT_KEY, sortIds(options), "similarity");
export const rememberSort = (id: string) => remember(SORT_KEY, id);

const title = (document: DocumentRecord) => text(document.title) || displayName(document.name);
const added = (document: DocumentRecord) => document.created_at ?? document.updated_at;

export const SORTS: readonly Option<DocumentRecord>[] = [
  { id: "similarity", label: "Similarity" },
  { id: "newest", label: "Newest added", compare: (a, b) => byTime(added(a), added(b), -1) },
  { id: "oldest", label: "Oldest added", compare: (a, b) => byTime(added(a), added(b)) },
  { id: "dated-newest", label: "Document date, newest", compare: (a, b) => byTime(a.document_date, b.document_date, -1) },
  { id: "dated-oldest", label: "Document date, oldest", compare: (a, b) => byTime(a.document_date, b.document_date) },
  { id: "title", label: "Title, A to Z", compare: (a, b) => byText(title(a), title(b)) },
  { id: "title-desc", label: "Title, Z to A", compare: (a, b) => byText(title(a), title(b), -1) },
  { id: "name", label: "File name", compare: (a, b) => byText(displayName(a.name), displayName(b.name)) },
  { id: "type", label: "Type", compare: (a, b) => byText(text(a.doc_type), text(b.doc_type)) || byText(title(a), title(b)) },
  { id: "largest", label: "Largest file", compare: (a, b) => byNumber(a.size_bytes, b.size_bytes, -1) },
  { id: "smallest", label: "Smallest file", compare: (a, b) => byNumber(a.size_bytes, b.size_bytes) },
  { id: "pages", label: "Most pages", compare: (a, b) => byNumber(a.page_count, b.page_count, -1) },
];
