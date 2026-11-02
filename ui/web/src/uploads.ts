/** Upload queue state: lives above the dialog, so closing it never stops an upload. */

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { uploadDocument, type DocumentRecord, type UploadOptions } from "./api";
import { messageOf, storedName } from "./lib";

export type UploadResult = DocumentRecord & { id: number };
export type FailedUpload = { id: number; file: File; options: UploadOptions; message: string };
export type UploadProgress = { name: string; fraction: number; elapsed: number; remaining: number };

type Queued = { file: File; options: UploadOptions };

const readBase64 = (file: File) =>
  new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(reader.error ?? new Error("The file could not be read."));
    reader.readAsDataURL(file);
  });

export function useUploads() {
  const client = useQueryClient();
  const [results, setResults] = useState<UploadResult[]>([]);
  const [failed, setFailed] = useState<FailedUpload[]>([]);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const queue = useRef<Queued[]>([]);
  const running = useRef(false);
  const nextId = useRef(0);
  const batch = useRef({ total: 0, done: 0 });
  const startedAt = useRef(0);

  // Show time passing during the long, silent reading of every page.
  const active = progress !== null;
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => {
      setProgress((current) => current && { ...current, elapsed: Math.floor((Date.now() - startedAt.current) / 1000) });
    }, 1000);
    return () => clearInterval(timer);
  }, [active]);

  const drain = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    while (queue.current.length) {
      const { file, options } = queue.current.shift()!;
      startedAt.current = Date.now();
      setProgress({
        name: file.name,
        fraction: batch.current.total ? batch.current.done / batch.current.total : 0,
        elapsed: 0,
        remaining: queue.current.length,
      });
      const id = nextId.current++;
      try {
        const metadata = await uploadDocument(storedName(file.name), await readBase64(file), options);
        setResults((current) => [...current, { ...metadata, id }]);
        client.invalidateQueries({ queryKey: ["documents"] });
      } catch (failure) {
        setFailed((current) => [...current, { id, file, options, message: messageOf(failure, "The upload failed.") }]);
      }
      batch.current.done += file.size;
    }
    batch.current = { total: 0, done: 0 };
    running.current = false;
    setProgress(null);
  }, [client]);

  const add = useCallback(
    (files: File[], options: UploadOptions) => {
      queue.current.push(...files.map((file) => ({ file, options })));
      batch.current.total += files.reduce((sum, file) => sum + file.size, 0);
      void drain();
    },
    [drain],
  );

  const retry = useCallback(
    (upload: FailedUpload) => {
      setFailed((current) => current.filter((item) => item.id !== upload.id));
      add([upload.file], upload.options);
    },
    [add],
  );

  const clear = useCallback(() => setResults([]), []);

  return { results, failed, progress, add, retry, clear };
}

export type Uploads = ReturnType<typeof useUploads>;
