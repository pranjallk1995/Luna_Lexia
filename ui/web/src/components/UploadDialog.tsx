import { Cloud, Laptop, Mail, RefreshCw, UploadCloud } from "lucide-react";
import { useState, type DragEvent } from "react";

import { useDocumentTypes, type Settings } from "../api";
import { displayName } from "../lib";
import type { Uploads } from "../uploads";
import { Dialog } from "./Dialog";
import { DrivePanel } from "./DrivePanel";
import { GmailPanel } from "./GmailPanel";
import { useToast } from "./Toasts";

type Props = {
  uploads: Uploads;
  settings: Settings;
  onClose: () => void;
};

function DeviceUpload({ uploads, settings }: Omit<Props, "onClose">) {
  const toast = useToast();
  const { types, label: typeLabel } = useDocumentTypes();
  const [dragging, setDragging] = useState(false);
  const [password, setPassword] = useState("");
  const [docType, setDocType] = useState("auto");
  const { results, failed, progress } = uploads;

  const accept = (files: File[]) => {
    const supported = files.filter((file) => settings.upload_types.includes(file.name.split(".").pop()?.toLowerCase() ?? ""));
    if (supported.length < files.length) toast(`Only ${settings.upload_types.join(", ")} files can be uploaded.`);
    if (supported.length) {
      uploads.add(supported, { ...(password && { password }), ...(docType !== "auto" && { doc_type: docType }) });
    }
  };

  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    accept(Array.from(event.dataTransfer.files));
  };

  return (
    <>
      <div className="lx-upload-options">
        <label className="lv-field">
          PDF password (if protected)
          <input className="lv-input" type="password" autoComplete="off" value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        <label className="lv-field">
          Document type
          <select className="lv-select" value={docType} onChange={(event) => setDocType(event.target.value)}>
            <option value="auto">Detect automatically</option>
            {types.map((type) => (
              <option value={type.doc_type} key={type.doc_type}>
                {type.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label
        className={`lv-dropzone${dragging ? " lv-dropzone-active" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <UploadCloud size={26} />
        <span>Drag and drop PDFs, photos of documents, or Word and Excel files, or click to choose</span>
        <small>{settings.upload_types.join(", ").toUpperCase()}</small>
        <input
          type="file"
          multiple
          accept={settings.upload_types.map((type) => `.${type}`).join(",")}
          onChange={(event) => {
            accept(Array.from(event.target.files ?? []));
            event.target.value = "";
          }}
        />
      </label>

      {progress && (
        <div className="lv-progress" role="status">
          <div className="lv-progress-text">
            Reading and extracting {progress.name}. Time: {progress.elapsed}s elapsed
            {progress.remaining > 0 && `, ${progress.remaining} more waiting`}
          </div>
          <div className="lv-progress-track">
            <div className="lv-progress-bar" style={{ width: `${Math.max(progress.fraction * 100, 4)}%` }} />
          </div>
        </div>
      )}

      {failed.map((upload) => (
        <div className="lv-alert lv-alert-error lv-upload-failed" key={upload.id}>
          <span>
            Could not upload {upload.file.name}: {upload.message}
          </span>
          <button className="lv-button" type="button" onClick={() => uploads.retry(upload)}>
            Retry
          </button>
        </div>
      ))}

      {results.map((result) => (
        <article className="lv-upload-result" key={result.id}>
          <div className="lv-card-name">{displayName(result.name)}</div>
          {result.status === "duplicate" && <p className="lv-alert">{result.message ?? "This document is already stored."}</p>}
          <p>
            <strong>{result.title}</strong>{" "}
            {[typeLabel(result.doc_type), result.financial_year && `FY ${result.financial_year}`, result.issuer].filter(Boolean).map((fact) => `· ${fact} `)}
          </p>
          <p>{result.summary}</p>
          <small>{(result.tags ?? []).map((tag) => `#${tag}`).join("  ")}</small>
          {(result.review_flags ?? []).map((flag) => (
            <p className="lv-review-flag lx-flag-line" key={flag}>
              {flag}
            </p>
          ))}
        </article>
      ))}

      {results.length > 0 && (
        <button className="lv-button lv-button-wide" type="button" onClick={uploads.clear}>
          <RefreshCw size={16} />
          Clear
        </button>
      )}
    </>
  );
}

export function UploadDialog({ uploads, settings, onClose }: Props) {
  const [source, setSource] = useState<"device" | "drive" | "gmail">("device");
  return (
    <Dialog title="Upload documents" size="large" onClose={onClose}>
      <div className="lv-tabs" role="tablist">
        <button className={source === "device" ? "lv-tab lv-tab-active" : "lv-tab"} type="button" role="tab" aria-selected={source === "device"} onClick={() => setSource("device")}>
          <Laptop size={15} /> This device
        </button>
        <button className={source === "drive" ? "lv-tab lv-tab-active" : "lv-tab"} type="button" role="tab" aria-selected={source === "drive"} onClick={() => setSource("drive")}>
          <Cloud size={15} /> Google Drive
        </button>
        <button className={source === "gmail" ? "lv-tab lv-tab-active" : "lv-tab"} type="button" role="tab" aria-selected={source === "gmail"} onClick={() => setSource("gmail")}>
          <Mail size={15} /> Gmail
        </button>
      </div>
      {source === "device" && <DeviceUpload uploads={uploads} settings={settings} />}
      {source === "drive" && <DrivePanel settings={settings} />}
      {source === "gmail" && <GmailPanel settings={settings} />}
    </Dialog>
  );
}
