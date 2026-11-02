import { Bot, Check, FileText, Heading, ListChecks, Maximize2, Minimize2, Pencil, Plus, RefreshCw, Tag, Text, Trash2, X, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type PointerEvent, type ReactNode } from "react";

import { pageUrl, useDocumentTypes, type DocumentActions, type DocumentRecord, type DocumentType, type Section, type Settings } from "../api";
import { displayName, messageOf, normalizedTag, splitName } from "../lib";
import { VALUE_COLUMN, cell, fieldLabel, isListField, itemColumns, tableRows, tableValues, type Row } from "../tables";
import { Dialog } from "./Dialog";
import { useToast } from "./Toasts";

function DetailRow({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <div className="lv-detail">
      <span className="lv-detail-icon">{icon}</span>
      <div className="lv-detail-content">{children}</div>
    </div>
  );
}

type EditableProps = {
  label: string;
  value: string;
  maxLength: number;
  placeholder?: string;
  /** Resolve to close the editor; reject to keep it open with the error. */
  save: (draft: string) => Promise<unknown>;
};

/** Text with a pencil: Enter or the checkmark saves, Escape cancels. */
function EditableText({ label, value, maxLength, placeholder, save }: EditableProps) {
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await save(draft ?? "");
      setDraft(null);
    } catch (failure) {
      setError(messageOf(failure, `Could not update ${label.toLowerCase()}.`));
    } finally {
      setBusy(false);
    }
  };

  if (draft === null) {
    return (
      <div className="lv-detail-line">
        <span className="lv-detail-text">{value}</span>
        <button className="lv-icon-button" type="button" aria-label={`Edit ${label.toLowerCase()}`} onClick={() => setDraft(value)}>
          <Pencil size={15} />
        </button>
      </div>
    );
  }
  return (
    <form onSubmit={submit}>
      <div className="lv-detail-line">
        <input
          className="lv-input"
          aria-label={label}
          autoFocus
          value={draft}
          maxLength={maxLength}
          placeholder={placeholder}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              // Cancel only this edit, not the whole dialog.
              event.preventDefault();
              event.stopPropagation();
              setDraft(null);
              setError("");
            }
          }}
        />
        <button className="lv-icon-button" type="submit" aria-label={`Save ${label.toLowerCase()}`} disabled={busy}>
          <Check size={16} />
        </button>
      </div>
      {error && <p className="lv-alert lv-alert-error">{error}</p>}
    </form>
  );
}

type TagsProps = {
  document: DocumentRecord;
  maxTags: number;
  actions: DocumentActions;
};

function TagEditor({ document, maxTags, actions }: TagsProps) {
  const toast = useToast();
  const [draft, setDraft] = useState<string | null>(null);
  const tags = document.tags ?? [];

  const removeTag = (tag: string) =>
    actions.updateMetadata.mutate(
      { name: document.name, change: { remove_tags: [tag] } },
      { onError: (failure) => toast(messageOf(failure, "Could not remove the tag.")) },
    );

  const addTag = (event: FormEvent) => {
    event.preventDefault();
    const tag = (draft ?? "").trim();
    if (!tag) return toast("Enter a tag name before adding it.");
    if (tags.some((existing) => normalizedTag(existing) === normalizedTag(tag))) {
      return toast("That tag is already on this document.");
    }
    if (tags.length >= maxTags) {
      return toast(`Maximum of ${maxTags} tags reached. Remove a tag before adding another.`);
    }
    actions.updateMetadata.mutate(
      { name: document.name, change: { add_tags: [tag] } },
      {
        onSuccess: () => setDraft(null),
        onError: () => toast("The tag could not be saved. Please try again."),
      },
    );
  };

  return (
    <div className="lv-detail-line lv-detail-line-top">
      <div className="lv-preview-tags">
        {tags.map((tag, index) => (
          <button
            className={`lv-tag lv-tag-${index % 6} lv-tag-removable`}
            type="button"
            key={tag}
            aria-label={`Remove tag ${tag}`}
            onClick={() => removeTag(tag)}
          >
            {tag}
            <X size={12} />
          </button>
        ))}
      </div>
      {draft === null ? (
        <button className="lv-icon-button" type="button" aria-label="Add a tag" onClick={() => setDraft("")}>
          <Plus size={16} />
        </button>
      ) : (
        <form className="lv-tag-form" onSubmit={addTag}>
          <input
            className="lv-input"
            aria-label="New tag"
            autoFocus
            placeholder="Tag name"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                setDraft(null);
              }
            }}
          />
          <button className="lv-icon-button" type="submit" aria-label="Save tag">
            <Check size={16} />
          </button>
        </form>
      )}
    </div>
  );
}

/** One page at a time; a zoomed page scrolls inside its frame and can be dragged by hand. */
function PageViewer({ document, zoomLevels, expanded, onExpand }: { document: DocumentRecord; zoomLevels: number[]; expanded: boolean; onExpand: () => void }) {
  const pages = document.page_count || 1;
  const [page, setPage] = useState(1);
  const [zoomIndex, setZoomIndex] = useState(0);
  const frame = useRef<HTMLDivElement>(null);
  const start = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const zoom = zoomLevels[zoomIndex];
  const shown = Math.min(Math.max(page, 1), pages);

  const grab = (event: PointerEvent<HTMLDivElement>) => {
    const element = frame.current;
    if (!element || zoomIndex === 0) return;
    start.current = { x: event.clientX, y: event.clientY, left: element.scrollLeft, top: element.scrollTop };
    element.setPointerCapture(event.pointerId);
    element.classList.add("lx-dragging");
  };
  const drag = (event: PointerEvent<HTMLDivElement>) => {
    if (!start.current || !frame.current) return;
    frame.current.scrollLeft = start.current.left - (event.clientX - start.current.x);
    frame.current.scrollTop = start.current.top - (event.clientY - start.current.y);
  };
  const release = () => {
    start.current = null;
    frame.current?.classList.remove("lx-dragging");
  };

  useEffect(() => {
    frame.current?.scrollTo(0, 0);
  }, [shown]);

  return (
    <div className="lx-pages">
      <div className="lx-page-tools">
        {pages > 1 && (
          <label className="lx-page-picker">
            <input
              className="lv-input"
              type="number"
              aria-label="Page"
              min={1}
              max={pages}
              step={1}
              value={page}
              onChange={(event) => setPage(Number(event.target.value))}
              onBlur={() => setPage(shown)}
            />
            <span>of {pages}</span>
          </label>
        )}
        <button className="lv-icon-button" type="button" aria-label="Zoom out" disabled={zoomIndex === 0} onClick={() => setZoomIndex(zoomIndex - 1)}>
          <ZoomOut size={17} />
        </button>
        <output className="lx-zoom-level">{zoom}%</output>
        <button className="lv-icon-button" type="button" aria-label="Zoom in" disabled={zoomIndex === zoomLevels.length - 1} onClick={() => setZoomIndex(zoomIndex + 1)}>
          <ZoomIn size={17} />
        </button>
        <button className="lv-icon-button lx-expand" type="button" aria-label={expanded ? "Show details beside the page" : "Use the full width for the page"} onClick={onExpand}>
          {expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
        </button>
      </div>
      <div
        ref={frame}
        className={zoomIndex === 0 ? "lx-page-frame" : "lx-page-frame lx-page-zoomed"}
        onPointerDown={grab}
        onPointerMove={drag}
        onPointerUp={release}
        onPointerCancel={release}
      >
        <img
          src={pageUrl(document, shown - 1, zoomIndex === 0 ? "preview" : "zoom")}
          alt={`Page ${shown}`}
          draggable={false}
          style={{ width: `${zoom}%` }}
        />
      </div>
    </div>
  );
}

function ListField({ field, label, columns, values, onSave }: { field: string; label: string; columns: string[] | null; values: unknown; onSave: (values: unknown[]) => void }) {
  const names = columns ?? [VALUE_COLUMN];
  const [rows, setRows] = useState<Row[]>(() => tableRows(values, columns));
  const table = useRef<HTMLTableElement>(null);
  const saved = JSON.stringify(values ?? []);
  // A saved answer must not overwrite a cell someone is typing in.
  useEffect(() => {
    if (!table.current?.contains(window.document.activeElement)) setRows(tableRows(JSON.parse(saved), columns));
  }, [saved, columns?.join()]);

  const commit = (next: Row[]) => {
    const cleaned = tableValues(next, columns);
    if (JSON.stringify(cleaned) !== JSON.stringify(tableValues(tableRows(JSON.parse(saved), columns), columns))) onSave(cleaned);
  };

  return (
    <details className="lx-list-field">
      <summary>
        {label} ({tableValues(rows, columns).length})
      </summary>
      <div className="lv-table-scroll">
        <table className="lx-edit-table" ref={table}>
          <thead>
            <tr>
              {names.map((name) => (
                <th key={name}>{columns ? fieldLabel(name) : VALUE_COLUMN}</th>
              ))}
              <th aria-hidden="true" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr
                key={index}
                // Saved when focus leaves the row, so moving between its cells never sends half a row.
                onBlur={(event) => {
                  if (!event.currentTarget.contains(event.relatedTarget)) commit(rows);
                }}
              >
                {names.map((name) => (
                  <td key={name}>
                    <input
                      aria-label={`${label} row ${index + 1} ${columns ? fieldLabel(name) : VALUE_COLUMN}`}
                      value={row[name] ?? ""}
                      onChange={(event) => setRows(rows.map((other, at) => (at === index ? { ...other, [name]: event.target.value } : other)))}
                    />
                  </td>
                ))}
                <td>
                  <button
                    className="lv-icon-button"
                    type="button"
                    aria-label={`Delete ${label} row ${index + 1}`}
                    onClick={() => {
                      const next = rows.filter((_, at) => at !== index);
                      setRows(next);
                      commit(next);
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button className="lv-button lx-add-row" type="button" onClick={() => setRows([...rows, Object.fromEntries(names.map((name) => [name, ""]))])}>
        <Plus size={15} />
        Add a row to {field.replace(/_/g, " ")}
      </button>
    </details>
  );
}

function Details({ document, type, typeLabel, actions }: { document: DocumentRecord; type?: DocumentType; typeLabel: string; actions: DocumentActions }) {
  const [error, setError] = useState("");
  const schemas = type?.fields?.properties ?? {};
  const extracted = document.extracted ?? {};
  const scalars = Object.keys(schemas).filter((field) => !isListField(schemas[field]));
  const lists = Object.keys(schemas).filter((field) => isListField(schemas[field]));
  const facts = [
    typeLabel,
    document.financial_year ? `FY ${document.financial_year}` : null,
    document.issuer,
    document.confidence !== null && document.confidence !== undefined ? `${Math.round(document.confidence * 100)}% confident` : null,
  ].filter(Boolean);

  // The backend parses and validates whatever is typed; only changed fields are sent.
  const save = (change: Record<string, unknown>, label: string) => {
    setError("");
    actions.updateMetadata.mutate(
      { name: document.name, change: { extracted: change } },
      { onError: (failure) => setError(messageOf(failure, `Could not save the corrected ${label}.`)) },
    );
  };

  return (
    <section className="lx-details">
      <h3>Extracted details</h3>
      <div className="lv-card-facts">{facts.join(" · ")}</div>
      {document.extraction_error && <p className="lv-alert lv-alert-error">Extraction failed: {document.extraction_error}</p>}
      {scalars.length > 0 && (
        <table className="lx-edit-table">
          <tbody>
            {scalars.map((field) => (
              <tr key={field}>
                <th scope="row">{fieldLabel(field)}</th>
                <td>
                  <input
                    key={`${field}-${cell(extracted[field])}`}
                    aria-label={fieldLabel(field)}
                    defaultValue={cell(extracted[field])}
                    onBlur={(event) => {
                      if (event.target.value !== cell(extracted[field])) {
                        save({ [field]: event.target.value.trim() || null }, "details");
                      }
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") event.currentTarget.blur();
                    }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {error && <p className="lv-alert lv-alert-error">{error}</p>}
      {lists.map((field) => (
        <ListField
          key={`${type?.doc_type}-${field}`}
          field={field}
          label={fieldLabel(field)}
          columns={itemColumns(type, field)}
          values={extracted[field]}
          onSave={(values) => save({ [field]: values }, fieldLabel(field).toLowerCase())}
        />
      ))}
    </section>
  );
}

function Reextract({ document, types, actions }: { document: DocumentRecord; types: DocumentType[]; actions: DocumentActions }) {
  const [docType, setDocType] = useState(document.doc_type || "other");
  const [error, setError] = useState("");
  const { reextract } = actions;

  const run = () => {
    setError("");
    reextract.mutate(
      { name: document.name, docType },
      { onError: (failure) => setError(messageOf(failure, "Could not re-extract the document.")) },
    );
  };

  return (
    <>
      <div className="lx-reextract">
        <select className="lv-select" aria-label="Document type" value={docType} onChange={(event) => setDocType(event.target.value)}>
          {types.map((type) => (
            <option value={type.doc_type} key={type.doc_type}>
              {type.label}
            </option>
          ))}
        </select>
        <button className="lv-button" type="button" aria-label="Read the document again" disabled={reextract.isPending} onClick={run}>
          <RefreshCw size={16} className={reextract.isPending ? "lx-spin" : undefined} />
        </button>
      </div>
      {reextract.isPending && <p className="lv-caption">Re-reading the document with the local model…</p>}
      {error && <p className="lv-alert lv-alert-error">{error}</p>}
    </>
  );
}

type Props = {
  document: DocumentRecord;
  section: Section;
  settings: Settings;
  actions: DocumentActions;
  onClose: () => void;
};

/** A document's pages beside its metadata and extracted details, all editable in place. */
export function PreviewDialog({ document, section, settings, actions, onClose }: Props) {
  const toast = useToast();
  const { types, label: typeLabel } = useDocumentTypes();
  const [expanded, setExpanded] = useState(false);
  const shownName = displayName(document.name);
  const { stem: shownStem } = splitName(shownName);
  const { suffix } = splitName(document.name);
  const flags = document.review_flags ?? [];
  const type = types.find((candidate) => candidate.doc_type === (document.doc_type || "other"));

  const rename = async (draft: string) => {
    const stem = draft.trim();
    if (stem === shownStem) return;
    const newName = `${stem}${suffix}`;
    await actions.rename.mutateAsync({ name: document.name, newName });
    toast(`Renamed to ${newName}.`);
    // The file moved, so this dialog's document no longer exists under its old name.
    onClose();
  };

  const saveText = (field: "title" | "summary") => (value: string) =>
    actions.updateMetadata.mutateAsync({ name: document.name, change: { [field]: value } });

  const markReviewed = () =>
    actions.updateMetadata.mutate(
      { name: document.name, change: { mark_reviewed: true } },
      { onError: (failure) => toast(messageOf(failure, "Could not mark the document as reviewed.")) },
    );

  return (
    <Dialog title={shownName} size="large" onClose={onClose}>
      <div className={expanded ? "lx-document lx-document-expanded" : "lx-document"}>
        <PageViewer document={document} zoomLevels={settings.zoom_levels} expanded={expanded} onExpand={() => setExpanded(!expanded)} />
        <div className="lx-side">
          {section !== "hidden" && (
            <DetailRow icon={<FileText size={16} />}>
              <EditableText
                key={shownStem}
                label="Name"
                value={shownStem}
                maxLength={settings.name_max_chars}
                placeholder={`Letters, numbers, and underscores (${suffix} is kept)`}
                save={rename}
              />
            </DetailRow>
          )}
          <DetailRow icon={<Heading size={16} />}>
            <EditableText label="Title" value={document.title ?? ""} maxLength={settings.title_max_chars} save={saveText("title")} />
          </DetailRow>
          <DetailRow icon={<Text size={16} />}>
            <EditableText label="Summary" value={document.summary ?? ""} maxLength={settings.summary_max_chars} save={saveText("summary")} />
          </DetailRow>
          <DetailRow icon={<Tag size={16} />}>
            <TagEditor document={document} maxTags={settings.max_tags} actions={actions} />
          </DetailRow>
          {document.analysis_model && (
            <DetailRow icon={<Bot size={16} />}>
              <span className="lv-detail-text">Analyzed by {document.analysis_model} + Docling</span>
            </DetailRow>
          )}
          {flags.length > 0 && (
            <div className="lx-review">
              <div>
                <strong>Check these details before relying on them:</strong>
                <ul>
                  {flags.map((flag) => (
                    <li key={flag}>{flag}</li>
                  ))}
                </ul>
              </div>
              <button className="lv-button" type="button" aria-label="Mark as reviewed" onClick={markReviewed}>
                <ListChecks size={16} />
              </button>
            </div>
          )}
          <Details document={document} type={type} typeLabel={typeLabel(document.doc_type)} actions={actions} />
          <Reextract key={document.doc_type} document={document} types={types} actions={actions} />
        </div>
      </div>
    </Dialog>
  );
}
