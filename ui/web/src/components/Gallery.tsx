import { EyeOff, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { thumbnailUrl, useDocumentActions, useDocuments, useDocumentTypes, type DocumentRecord, type Section, type Settings } from "../api";
import { displayName, matching, messageOf, pageCount, pageOf } from "../lib";
import { ConfirmDialog } from "./ConfirmDialog";
import { DocumentCard, type CardAction } from "./DocumentCard";
import { Pager } from "./Pager";
import { PreviewDialog } from "./PreviewDialog";
import { SearchBox } from "./SearchBox";
import { useToast } from "./Toasts";

const COPY: Record<Section, { label: string; noMatch: string }> = {
  view: { label: "Search documents", noMatch: "No documents match your search." },
  favorites: { label: "Search favorite documents", noMatch: "No favorite documents match your search." },
  hidden: { label: "Search hidden documents", noMatch: "No hidden documents match your search." },
};

type Pending = { kind: "delete" | "hide"; document: DocumentRecord } | null;

type Props = {
  section: Section;
  layout: "grid" | "list";
  settings: Settings;
};

export function Gallery({ section, layout, settings }: Props) {
  const { data, isPending, error } = useDocuments(section);
  const actions = useDocumentActions(section);
  const toast = useToast();
  const { label: typeLabel } = useDocumentTypes();
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [openName, setOpenName] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending>(null);

  const filtered = useMemo(() => matching(section === "favorites" ? (data ?? []).filter((document) => document.favorite) : (data ?? []), query), [data, section, query]);
  const count = pageCount(filtered.length, settings.documents_per_page);
  const current = Math.min(page, count - 1);
  const documents = pageOf(filtered, current, settings.documents_per_page);
  const openIndex = filtered.findIndex((document) => document.name === openName);

  // Fetch the next page's thumbnails while idle, so paging forward shows them at once.
  useEffect(() => {
    const next = pageOf(filtered, current + 1, settings.documents_per_page);
    const handle = setTimeout(() => next.forEach((document) => (new Image().src = thumbnailUrl(document))), 300);
    return () => clearTimeout(handle);
  }, [filtered, current, settings.documents_per_page]);

  const { setFavorite, restore } = actions;
  const onAction = useCallback(
    (action: CardAction, document: DocumentRecord) => {
      if (action === "open") setOpenName(document.name);
      if (action === "delete" || action === "hide") setPending({ kind: action, document });
      if (action === "favorite") {
        setFavorite.mutate(
          { name: document.name, favorite: !document.favorite },
          { onError: (failure) => toast(messageOf(failure, "Could not update Favorite status.")) },
        );
      }
      if (action === "restore") {
        restore.mutate(document.name, { onError: (failure) => toast(messageOf(failure, "Could not restore the document.")) });
      }
    },
    // Mutation objects change identity as they run; their mutate functions do not.
    [setFavorite.mutate, restore.mutate, toast],
  );

  const changeQuery = (value: string) => {
    setQuery(value);
    setPage(0);
  };

  const changePage = (target: number) => {
    setPage(target);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  // The gallery follows along behind the viewer, so closing it lands on the document last seen.
  const stepViewer = (delta: number) => {
    const index = (openIndex + delta + filtered.length) % filtered.length;
    setOpenName(filtered[index].name);
    setPage(Math.floor(index / settings.documents_per_page));
  };

  return (
    <>
      <SearchBox label={COPY[section].label} value={query} count={data ? filtered.length : undefined} onChange={changeQuery} />
      {error && <p className="lv-alert lv-alert-error">{error.message}</p>}
      {isPending && <div className="lv-skeleton-grid" aria-label="Loading documents" />}
      {data && !filtered.length && (
        <p className="lv-alert">{query.trim() ? COPY[section].noMatch : "No documents are currently stored in Luna Lexia."}</p>
      )}
      {documents.length > 0 && (
        <>
          <div className={`lv-gallery lv-gallery-${layout}`} key={`${section}-${current}`}>
            {documents.map((document) => (
              <DocumentCard key={document.name} document={document} section={section} layout={layout} typeLabel={typeLabel(document.doc_type)} onAction={onAction} />
            ))}
          </div>
          <Pager page={current} count={count} onChange={changePage} />
        </>
      )}
      {openIndex >= 0 && (
        <PreviewDialog
          document={filtered[openIndex]}
          position={openIndex + 1}
          total={filtered.length}
          next={filtered[(openIndex + 1) % filtered.length]}
          section={section}
          settings={settings}
          actions={actions}
          onStep={stepViewer}
          onAction={onAction}
          onClose={() => setOpenName(null)}
        />
      )}
      {pending?.kind === "delete" && (
        <ConfirmDialog
          title="Delete document?"
          message="Permanently delete"
          subject={displayName(pending.document.name)}
          action="Delete"
          icon={Trash2}
          run={() => actions.remove.mutateAsync(pending.document.name)}
          onClose={() => setPending(null)}
        />
      )}
      {pending?.kind === "hide" && (
        <ConfirmDialog
          title="Hide document?"
          message="Move to Hidden Documents:"
          subject={displayName(pending.document.name)}
          action="Hide"
          icon={EyeOff}
          run={() => actions.hide.mutateAsync(pending.document.name)}
          onClose={() => setPending(null)}
        />
      )}
    </>
  );
}
