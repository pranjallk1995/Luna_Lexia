/** The gateway's routes, and React Query hooks that keep galleries in step with them. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { post, remove, request } from "./http";
import { inBatches, type BatchFailure } from "./lib";

export type Section = "view" | "favorites" | "hidden";

export type DocumentRecord = {
  name: string;
  title?: string | null;
  summary?: string | null;
  tags?: string[] | null;
  favorite?: boolean;
  analysis_model?: string | null;
  doc_type?: string | null;
  financial_year?: string | null;
  issuer?: string | null;
  confidence?: number | null;
  page_count?: number | null;
  review_flags?: string[] | null;
  extraction_error?: string | null;
  extracted?: Record<string, unknown> | null;
  size_bytes: number;
  version: number;
  status?: string;
  message?: string;
};

export type Settings = {
  documents_per_page: number;
  batch_size: number;
  max_tags: number;
  name_max_chars: number;
  title_max_chars: number;
  summary_max_chars: number;
  upload_types: string[];
  zoom_levels: number[];
  drive_refresh_seconds: number;
  recent_imports: number;
};

export type MetadataChange = {
  title?: string;
  summary?: string;
  caption?: string;
  add_tags?: string[];
  remove_tags?: string[];
  extracted?: Record<string, unknown>;
  mark_reviewed?: boolean;
};

export type NamedMetadataChange = MetadataChange & { name: string };

export type UploadOptions = { password?: string; doc_type?: string };

const segment = encodeURIComponent;

export const thumbnailUrl = (document: DocumentRecord) =>
  // Browsers keep thumbnails for a year, so a new shape needs a new URL: change `shape` with THUMBNAIL_SIZE.
  `/app/thumbnails/${segment(document.name)}?v=${document.version}&shape=3x2`;

export const pageUrl = (document: DocumentRecord, index: number, size: "preview" | "zoom") =>
  `/app/pages/${segment(document.name)}/${index}?size=${size}&v=${document.version}`;

export const fileUrl = (document: DocumentRecord) => `/app/files/${segment(document.name)}?v=${document.version}`;

export const downloadUrl = (document: DocumentRecord, fileName: string) =>
  `${fileUrl(document)}&download=${segment(fileName)}`;

export const uploadDocument = (name: string, contentBase64: string, options: UploadOptions) =>
  post<DocumentRecord>("/api/documents", { name, content_base64: contentBase64, ...options });

type Collection = "visible" | "hidden";

const collectionOf = (section: Section): Collection => (section === "hidden" ? "hidden" : "visible");

export function useDocuments(section: Section) {
  const collection = collectionOf(section);
  return useQuery({
    queryKey: ["documents", collection],
    queryFn: async () => {
      const { documents } = await request<{ documents: DocumentRecord[] }>(
        collection === "hidden" ? "/app/hidden" : "/app/documents",
      );
      return documents;
    },
  });
}

/** Every change to one collection: applied on screen first, undone if the server refuses it. */
export function useDocumentActions(section: Section, batchSize: number) {
  const collection = collectionOf(section);
  const client = useQueryClient();
  const key = ["documents", collection];
  const prefix = collection === "hidden" ? "/api/hidden/documents" : "/api/documents";

  const refresh = () => client.invalidateQueries({ queryKey: ["documents"] });

  const change = <V, R>(
    run: (variables: V) => Promise<R>,
    apply: (documents: DocumentRecord[], variables: V) => DocumentRecord[],
  ) => ({
    mutationFn: run,
    onMutate: async (variables: V) => {
      await client.cancelQueries({ queryKey: key });
      const previous = client.getQueryData<DocumentRecord[]>(key);
      if (previous) client.setQueryData(key, apply(previous, variables));
      return { previous };
    },
    onError: (_error: unknown, _variables: V, context?: { previous?: DocumentRecord[] }) => {
      if (context?.previous) client.setQueryData(key, context.previous);
    },
    onSettled: refresh,
  });

  const without = (documents: DocumentRecord[], name: string) => documents.filter((document) => document.name !== name);
  const withoutAll = (documents: DocumentRecord[], names: string[]) => {
    const leaving = new Set(names);
    return documents.filter((document) => !leaving.has(document.name));
  };

  const sameName = (name: string) => name;

  /** Post names a batch at a time to a route that changes all of a batch or none of it. */
  const postNames = (path: string, names: string[], extra?: object) =>
    inBatches(names, batchSize, sameName, async (batch) => {
      await post(path, { names: batch, ...extra });
      return [];
    });

  const deleteNames = (names: string[]) =>
    inBatches(names, batchSize, sameName, async (batch) => {
      const path = collection === "hidden" ? "/api/hidden/delete" : "/api/documents/delete";
      return (await post<{ failed: BatchFailure[] }>(path, { names: batch })).failed;
    });

  const setFavorites = useMutation(
    change(
      ({ names, favorite }: { names: string[]; favorite: boolean }) => postNames("/api/favorites", names, { favorite }),
      (documents, { names, favorite }) => {
        const changing = new Set(names);
        return documents.map((document) => (changing.has(document.name) ? { ...document, favorite } : document));
      },
    ),
  );

  const removeMany = useMutation(change(deleteNames, withoutAll));

  const hideMany = useMutation(change((names: string[]) => postNames("/api/hidden/hide", names), withoutAll));

  const restoreMany = useMutation(change((names: string[]) => postNames("/api/hidden/restore", names), withoutAll));

  const updateMetadataMany = useMutation({
    mutationFn: (changes: NamedMetadataChange[]) =>
      inBatches(
        changes,
        batchSize,
        (item) => item.name,
        async (batch) => (await post<{ failed: BatchFailure[] }>(`${prefix}/metadata`, { changes: batch })).failed,
      ),
    onSettled: refresh,
  });

  const setFavorite = useMutation(
    change(
      ({ name, favorite }: { name: string; favorite: boolean }) => post("/api/favorites", { names: [name], favorite }),
      (documents, { name, favorite }) =>
        documents.map((document) => (document.name === name ? { ...document, favorite } : document)),
    ),
  );

  const removeDocument = useMutation(
    change(
      (name: string): Promise<unknown> =>
        collection === "hidden" ? deleteNames([name]) : remove(`/api/documents/${segment(name)}`),
      without,
    ),
  );

  const hide = useMutation(change((name: string) => post("/api/hidden/hide", { names: [name] }), without));

  const restore = useMutation(change((name: string) => post("/api/hidden/restore", { names: [name] }), without));

  const rename = useMutation({
    mutationFn: ({ name, newName }: { name: string; newName: string }) =>
      post(`/api/documents/${segment(name)}/rename`, { new_name: newName }),
    onSuccess: refresh,
  });

  // Everything but the file's own version, which the saved record does not know.
  const merge = (name: string, saved: DocumentRecord) =>
    client.setQueryData<DocumentRecord[]>(key, (documents) =>
      documents?.map((document) =>
        document.name === name ? { ...document, ...saved, version: document.version, size_bytes: document.size_bytes } : document,
      ),
    );

  const updateMetadata = useMutation({
    mutationFn: ({ name, change: edit }: { name: string; change: MetadataChange }) =>
      post<DocumentRecord>(`${prefix}/${segment(name)}/metadata`, edit),
    onSuccess: (saved, { name }) => merge(name, saved),
  });

  const reextract = useMutation({
    mutationFn: ({ name, docType }: { name: string; docType: string }) =>
      post<DocumentRecord>(`${prefix}/${segment(name)}/reextract`, { doc_type: docType }),
    onSuccess: (saved, { name }) => merge(name, saved),
  });

  return {
    setFavorite,
    remove: removeDocument,
    hide, restore, rename, updateMetadata, reextract,
    setFavorites,
    removeMany,
    hideMany,
    restoreMany,
    updateMetadataMany,
  };
}

export type DocumentActions = ReturnType<typeof useDocumentActions>;

// Document types

export type FieldSchema = {
  type?: string;
  anyOf?: FieldSchema[];
  items?: FieldSchema & { $ref?: string; properties?: Record<string, unknown> };
};

export type DocumentType = {
  doc_type: string;
  label: string;
  fields?: { properties?: Record<string, FieldSchema>; $defs?: Record<string, { properties?: Record<string, unknown> }> };
};

export function useDocumentTypes() {
  const { data } = useQuery({
    queryKey: ["document-types"],
    queryFn: () => request<{ document_types: DocumentType[] }>("/api/document-types"),
    staleTime: Infinity,
  });
  const types = data?.document_types ?? [];
  const label = useCallback(
    (docType?: string | null) => types.find((type) => type.doc_type === docType)?.label ?? "Document",
    [types],
  );
  return { types, label };
}

// Google Drive and bulk imports

export type DriveStatus = { configured: boolean; connected: boolean; email?: string; sign_in_expired?: boolean };
export type DriveFolder = { folder_id: string; name: string };

export type GmailAttachment = { source_id: string; name: string; subject: string; sender: string; date: string; imported: boolean };
export type GmailPreview = { query: string; attachments: GmailAttachment[]; more: boolean };

export type ImportItem = {
  source?: string | null;
  name: string;
  path?: string | null;
  outcome: string;
  error?: string | null;
  document_name?: string | null;
  source_id?: string | null;
};

export type ImportJob = {
  job_id: string;
  source: string;
  status: string;
  total?: number | null;
  processed?: number | null;
  outcomes?: Record<string, number> | null;
  error?: string | null;
  retryable?: boolean;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  current_item?: string | null;
  current_started_at?: string | null;
  items?: ImportItem[];
};

export const useDriveStatus = () =>
  useQuery({ queryKey: ["drive-status"], queryFn: () => request<DriveStatus>("/api/drive/status"), staleTime: 0 });

export const useDriveLink = (enabled: boolean) =>
  useQuery({
    queryKey: ["drive-link"],
    queryFn: () => request<{ url: string }>("/api/drive/authorization-url"),
    enabled,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });

export const useGmailStatus = () =>
  useQuery({ queryKey: ["gmail-status"], queryFn: () => request<DriveStatus>("/api/gmail/status"), staleTime: 0 });

export const useGmailLink = () =>
  useQuery({
    queryKey: ["gmail-link"],
    queryFn: () => request<{ url: string }>("/api/gmail/authorization-url"),
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });

export const useDriveFolders = (enabled: boolean) =>
  useQuery({
    queryKey: ["drive-folders"],
    queryFn: async () => (await request<{ folders: DriveFolder[] }>("/api/drive/folders")).folders,
    enabled,
    staleTime: 5 * 60_000,
  });

export const useImportJobs = (limit: number, refreshSeconds: number, enabled: boolean) =>
  useQuery({
    queryKey: ["imports"],
    queryFn: async () => (await request<{ jobs: ImportJob[] }>(`/api/imports?limit=${limit}`)).jobs,
    enabled,
    refetchInterval: refreshSeconds * 1000,
    staleTime: 0,
  });

/** A job's per-file report; a finished job's never changes, so it is not fetched again. */
export const useImportReport = (jobId: string, active: boolean, refreshSeconds: number) =>
  useQuery({
    queryKey: ["import", jobId, active],
    queryFn: () => request<ImportJob>(`/api/imports/${jobId}`),
    refetchInterval: active ? refreshSeconds * 1000 : false,
    staleTime: active ? 0 : Infinity,
  });

export const drive = {
  disconnect: () => post("/api/drive/disconnect"),
  importFolder: (folder: string, recursive: boolean, docType: string | null) =>
    post<{ source: string }>("/api/imports/drive-folder", { folder, recursive, doc_type: docType }),
  retry: (jobId: string) => post<{ source: string }>(`/api/imports/${jobId}/retry`),
  batch: (items: (({ drive_file_id: string } | { gmail_attachment: string }) & { password: string })[]) => post("/api/imports/batch", { items }),
};

export const gmail = {
  disconnect: () => post("/api/gmail/disconnect"),
  preview: (query: string) => post<GmailPreview>("/api/gmail/preview", { query }),
  importSearch: (query: string, docType: string | null, password: string, passwordDetails: string) =>
    post<{ source: string }>("/api/imports/gmail", { query, doc_type: docType, password, password_details: passwordDetails }),
};
