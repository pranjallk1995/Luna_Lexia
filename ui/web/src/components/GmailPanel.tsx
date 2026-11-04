import { useQueryClient } from "@tanstack/react-query";
import { CloudDownload, CloudOff, Mail, Search } from "lucide-react";
import { useState } from "react";

import { gmail, useDocumentTypes, useGmailLink, useGmailStatus, useImportJobs, type GmailPreview, type Settings } from "../api";
import { messageOf } from "../lib";
import { Job } from "./DrivePanel";
import { useToast } from "./Toasts";

function Connect({ label = "Connect Gmail" }: { label?: string }) {
  const link = useGmailLink();
  if (link.error) return <p className="lv-alert lv-alert-error">{messageOf(link.error, "Could not start the Google sign-in.")}</p>;
  return (
    <>
      <a className={link.data ? "lv-button lv-button-wide" : "lv-button lv-button-wide lx-disabled"} href={link.data?.url} target="_blank" rel="noopener">
        <Mail size={17} />
        {label}
      </a>
      <p className="lv-caption">Read-only access. After signing in, close that tab and come back here.</p>
    </>
  );
}

function Preview({ preview }: { preview: GmailPreview }) {
  if (!preview.attachments.length) return <p className="lv-alert">No importable attachments match this search.</p>;
  return (
    <>
      <p className="lv-caption">
        {preview.more ? `The first ${preview.attachments.length} attachments found; there are more.` : `${preview.attachments.length} attachment(s) found.`}
      </p>
      <div className="lv-table-scroll">
        <table className="lv-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>File</th>
              <th>Mail</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {preview.attachments.map((attachment) => (
              <tr key={attachment.source_id}>
                <td>{attachment.date}</td>
                <td>{attachment.name}</td>
                <td>{attachment.subject}</td>
                <td>{attachment.imported ? "Imported" : "New"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function GmailSearch({ refreshJobs }: { refreshJobs: () => void }) {
  const toast = useToast();
  const { types } = useDocumentTypes();
  const [query, setQuery] = useState("");
  const [details, setDetails] = useState("");
  const [password, setPassword] = useState("");
  const [docType, setDocType] = useState("auto");
  const [preview, setPreview] = useState<GmailPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const search = query.trim();

  const showPreview = async () => {
    setBusy(true);
    try {
      setPreview(await gmail.preview(search));
    } catch (failure) {
      setPreview(null);
      toast(messageOf(failure, "Could not search Gmail."));
    } finally {
      setBusy(false);
    }
  };

  const start = async () => {
    try {
      const job = await gmail.importSearch(search, docType === "auto" ? null : docType, password, details);
      toast(`Importing ${job.source}`);
      setPassword("");
      setDetails("");
      setPreview(null);
      refreshJobs();
    } catch (failure) {
      toast(messageOf(failure, "Could not start the import."));
    }
  };

  return (
    <div className="lv-form">
      <input
        className="lv-input"
        aria-label="Gmail search"
        placeholder='Search Gmail, e.g. from:icicibank.com "credit card" statement'
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setPreview(null);
        }}
      />
      <input
        className="lv-input"
        autoComplete="off"
        aria-label="Details for password rules"
        placeholder="For locked PDFs: name, date of birth, card's last digits"
        value={details}
        onChange={(event) => setDetails(event.target.value)}
      />
      <input
        className="lv-input"
        type="password"
        autoComplete="off"
        aria-label="Password for locked PDFs"
        placeholder="…or their password, if you know it"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
      />
      <select className="lv-select" aria-label="Document type" value={docType} onChange={(event) => setDocType(event.target.value)}>
        <option value="auto">Detect each type</option>
        {types.map((type) => (
          <option value={type.doc_type} key={type.doc_type}>
            {type.label}
          </option>
        ))}
      </select>
      <p className="lv-caption">Your details are used by Lexia's local model for this import only, and are not stored.</p>
      <div className="lx-gmail-actions">
        <button className="lv-button" type="button" disabled={!search || busy} onClick={showPreview}>
          <Search size={17} />
          {busy ? "Searching…" : "Preview"}
        </button>
        <button className="lv-button" type="button" disabled={!search} onClick={start}>
          <CloudDownload size={17} />
          Import all
        </button>
      </div>
      {preview && <Preview preview={preview} />}
    </div>
  );
}

export function GmailPanel({ settings }: { settings: Settings }) {
  const client = useQueryClient();
  const toast = useToast();
  const status = useGmailStatus();
  const connected = Boolean(status.data?.configured && status.data.connected);
  const jobs = useImportJobs(settings.recent_imports, settings.drive_refresh_seconds, connected);
  const refreshJobs = () => client.invalidateQueries({ queryKey: ["imports"] });

  if (status.error) return <p className="lv-alert lv-alert-error">{messageOf(status.error, "Could not reach Lexia.")}</p>;
  if (!status.data) return null;
  if (!status.data.configured) {
    return <p className="lv-alert">Gmail is not set up yet: set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Lexia's .env and restart it.</p>;
  }
  if (!status.data.connected) return <Connect />;

  const disconnect = async () => {
    try {
      await gmail.disconnect();
      client.invalidateQueries({ queryKey: ["gmail-status"] });
    } catch (failure) {
      toast(messageOf(failure, "Could not disconnect Gmail."));
    }
  };

  return (
    <div className="lx-drive">
      <div className="lx-reextract">
        <p className="lv-caption lx-account">Connected as {status.data.email}</p>
        <button className="lv-icon-button" type="button" aria-label="Disconnect Gmail" onClick={disconnect}>
          <CloudOff size={16} />
        </button>
      </div>
      {status.data.sign_in_expired ? (
        <div className="lv-form">
          <p className="lv-alert lv-alert-error">Your Gmail sign-in has expired. Reconnect to keep importing. Your documents and past imports are safe.</p>
          <Connect label="Reconnect Gmail" />
        </div>
      ) : (
        <GmailSearch refreshJobs={refreshJobs} />
      )}
      <h3 className="lx-jobs-title">Recent imports</h3>
      {jobs.error && <p className="lv-caption">Import progress is unavailable right now.</p>}
      {jobs.data && !jobs.data.length && <p className="lv-caption">No imports yet.</p>}
      {jobs.data?.map((job) => <Job job={job} settings={settings} refreshJobs={refreshJobs} key={job.job_id} />)}
    </div>
  );
}
