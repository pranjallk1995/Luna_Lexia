import { useQueryClient } from "@tanstack/react-query";
import { CloudDownload, CloudOff, HardDriveDownload, LockOpen, RotateCcw as Replay } from "lucide-react";
import { useState } from "react";

import {
  drive,
  useDocumentTypes,
  useDriveFolders,
  useDriveLink,
  useDriveStatus,
  useImportJobs,
  useImportReport,
  type ImportItem,
  type ImportJob,
  type Settings,
} from "../api";
import { messageOf, since } from "../lib";
import { useToast } from "./Toasts";

const ACTIVE_STATUSES = ["queued", "listing", "running"];
const OUTCOME_ICONS: Record<string, string> = {
  imported: "✅",
  duplicate: "♻️",
  skipped: "⏭️",
  needs_password: "🔒",
  unsupported: "➖",
  failed: "⚠️",
};

const outcomeLabel = (outcome: string) => outcome.replace(/_/g, " ");

function Connect({ enabled, label = "Connect Google Drive" }: { enabled: boolean; label?: string }) {
  const link = useDriveLink(enabled);
  if (link.error) return <p className="lv-alert lv-alert-error">{messageOf(link.error, "Could not start the Google sign-in.")}</p>;
  return (
    <>
      <a className={link.data ? "lv-button lv-button-wide" : "lv-button lv-button-wide lx-disabled"} href={link.data?.url} target="_blank" rel="noopener">
        <HardDriveDownload size={17} />
        {label}
      </a>
      <p className="lv-caption">Read-only access. After signing in, close that tab and reopen this dialog.</p>
    </>
  );
}

function Import({ refreshJobs }: { refreshJobs: () => void }) {
  const toast = useToast();
  const { types } = useDocumentTypes();
  const folders = useDriveFolders(true);
  const [picked, setPicked] = useState("");
  const [typed, setTyped] = useState("");
  const [recursive, setRecursive] = useState(true);
  const [docType, setDocType] = useState("auto");
  const folder = typed.trim() || picked;

  const start = async () => {
    try {
      const job = await drive.importFolder(folder, recursive, docType === "auto" ? null : docType);
      toast(`Importing ${job.source}`);
      refreshJobs();
    } catch (failure) {
      toast(messageOf(failure, "Could not start the import."));
    }
  };

  return (
    <div className="lv-form">
      {folders.error && <p className="lv-alert lv-alert-error">{messageOf(folders.error, "Could not list your Drive folders.")}</p>}
      <select className="lv-select" aria-label="Folder in My Drive" value={picked} onChange={(event) => setPicked(event.target.value)}>
        <option value="">Choose a folder in My Drive</option>
        {(folders.data ?? []).map(({ folder_id, name }) => (
          <option value={folder_id} key={folder_id}>
            {name}
          </option>
        ))}
      </select>
      <input
        className="lv-input"
        aria-label="Or a folder link, ID, or name"
        placeholder="…or paste a folder link, ID, or exact name"
        value={typed}
        onChange={(event) => setTyped(event.target.value)}
      />
      <div className="lx-import-row">
        <label className="lx-check">
          <input type="checkbox" checked={recursive} onChange={(event) => setRecursive(event.target.checked)} />
          Subfolders
        </label>
        <select className="lv-select" aria-label="Document type" value={docType} onChange={(event) => setDocType(event.target.value)}>
          <option value="auto">Detect each type</option>
          {types.map((type) => (
            <option value={type.doc_type} key={type.doc_type}>
              {type.label}
            </option>
          ))}
        </select>
        <button className="lv-button" type="button" aria-label="Import the folder" disabled={!folder} onClick={start}>
          <CloudDownload size={17} />
        </button>
      </div>
    </div>
  );
}

function Progress({ job, total, processed }: { job: ImportJob; total: number; processed: number }) {
  if (job.status === "queued") return <p className="lv-caption">Waiting for the import ahead of it · queued {since(job.created_at)} ago</p>;
  if (job.status === "listing") return <p className="lv-caption">Listing the folder · {since(job.started_at)} elapsed</p>;
  return (
    <div className="lv-progress">
      <div className="lv-progress-text">
        {processed} of {total} file(s) · {since(job.started_at)} elapsed
      </div>
      <div className="lv-progress-track">
        <div className="lv-progress-bar" style={{ width: `${total ? Math.min((processed / total) * 100, 100) : 0}%` }} />
      </div>
      {job.current_item && (
        <p className="lv-caption">
          Reading {job.current_item} · {since(job.current_started_at)}
        </p>
      )}
    </div>
  );
}

function PasswordRetry({ locked, refreshJobs }: { locked: ImportItem[]; refreshJobs: () => void }) {
  const toast = useToast();
  const [password, setPassword] = useState("");
  const retry = async () => {
    try {
      await drive.batch(locked.map((item) => ({ drive_file_id: item.source_id!, password })));
      toast(`Retrying ${locked.length} locked PDF(s)`);
      setPassword("");
      refreshJobs();
    } catch (failure) {
      toast(messageOf(failure, "Could not retry the locked PDFs."));
    }
  };
  return (
    <div className="lx-reextract">
      <input
        className="lv-input"
        type="password"
        autoComplete="off"
        aria-label="Password for the locked PDFs"
        placeholder={`Password for the ${locked.length} locked PDF(s)`}
        value={password}
        onChange={(event) => setPassword(event.target.value)}
      />
      <button className="lv-button" type="button" aria-label="Retry the locked PDFs" disabled={!password} onClick={retry}>
        <LockOpen size={16} />
      </button>
    </div>
  );
}

function Report({ job, active, settings, refreshJobs }: { job: ImportJob; active: boolean; settings: Settings; refreshJobs: () => void }) {
  const report = useImportReport(job.job_id, active, settings.drive_refresh_seconds);
  const items = report.data?.items ?? [];
  const locked = items.filter((item) => item.outcome === "needs_password" && item.source_id);
  return (
    <>
      {items.length > 0 && (
        <div className="lv-table-scroll">
          <table className="lv-table">
            <thead>
              <tr>
                <th aria-label="Outcome icon" />
                <th>File</th>
                <th>Outcome</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, index) => (
                <tr key={index}>
                  <td>{OUTCOME_ICONS[item.outcome] ?? ""}</td>
                  <td>
                    {item.path ?? ""}
                    {item.name}
                  </td>
                  <td>{outcomeLabel(item.outcome)}</td>
                  <td>{item.error || item.document_name || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {locked.length > 0 && <PasswordRetry locked={locked} refreshJobs={refreshJobs} />}
    </>
  );
}

function Job({ job, settings, refreshJobs }: { job: ImportJob; settings: Settings; refreshJobs: () => void }) {
  const toast = useToast();
  const active = ACTIVE_STATUSES.includes(job.status);
  const [open, setOpen] = useState(active);
  const total = job.total ?? 0;
  const processed = job.processed ?? 0;
  const outcomes = Object.entries(job.outcomes ?? {}).sort(([left], [right]) => left.localeCompare(right));
  const failed = job.outcomes?.failed ?? 0;

  const retry = async () => {
    try {
      const queued = await drive.retry(job.job_id);
      toast(`Queued ${queued.source}`);
      refreshJobs();
    } catch (failure) {
      toast(messageOf(failure, "Could not retry the import."));
    }
  };

  return (
    <details className="lx-job" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        {job.source} — {job.status} ({processed}/{total})
      </summary>
      <div className="lx-job-body">
        {active && <Progress job={job} total={total} processed={processed} />}
        {!active && job.finished_at && <p className="lv-caption">Took {since(job.started_at || job.created_at, job.finished_at)}</p>}
        {job.error && <p className="lv-alert lv-alert-error">{job.error}</p>}
        {outcomes.length > 0 && (
          <p className="lv-caption">
            {outcomes.map(([outcome, count]) => `${OUTCOME_ICONS[outcome] ?? ""} ${count} ${outcomeLabel(outcome)}`).join(" · ")}
          </p>
        )}
        {open && <Report job={job} active={active} settings={settings} refreshJobs={refreshJobs} />}
        {job.retryable && (
          <button className="lv-button lv-button-wide" type="button" onClick={retry}>
            <Replay size={16} />
            {failed ? `Retry ${failed} failed file(s)` : "Retry this import"}
          </button>
        )}
      </div>
    </details>
  );
}

export function DrivePanel({ settings }: { settings: Settings }) {
  const client = useQueryClient();
  const toast = useToast();
  const status = useDriveStatus();
  const connected = Boolean(status.data?.configured && status.data.connected);
  const jobs = useImportJobs(settings.recent_imports, settings.drive_refresh_seconds, connected);
  const refreshJobs = () => client.invalidateQueries({ queryKey: ["imports"] });

  if (status.error) return <p className="lv-alert lv-alert-error">{messageOf(status.error, "Could not reach Lexia.")}</p>;
  if (!status.data) return null;
  if (!status.data.configured) {
    return <p className="lv-alert">Google Drive is not set up yet: set DRIVE_CLIENT_ID and DRIVE_CLIENT_SECRET in Lexia's .env and restart it.</p>;
  }
  if (!status.data.connected) return <Connect enabled />;

  const disconnect = async () => {
    try {
      await drive.disconnect();
      client.removeQueries({ queryKey: ["drive-folders"] });
      client.invalidateQueries({ queryKey: ["drive-status"] });
    } catch (failure) {
      toast(messageOf(failure, "Could not disconnect Google Drive."));
    }
  };

  return (
    <div className="lx-drive">
      <div className="lx-reextract">
        <p className="lv-caption lx-account">Connected as {status.data.email}</p>
        <button className="lv-icon-button" type="button" aria-label="Disconnect Google Drive" onClick={disconnect}>
          <CloudOff size={16} />
        </button>
      </div>
      {status.data.sign_in_expired ? (
        <div className="lv-form">
          <p className="lv-alert lv-alert-error">
            Your Google Drive sign-in has expired. Reconnect to keep importing. Your documents and past imports are safe.
          </p>
          <Connect enabled label="Reconnect Google Drive" />
        </div>
      ) : (
        <Import refreshJobs={refreshJobs} />
      )}
      <h3 className="lx-jobs-title">Recent imports</h3>
      {jobs.error && <p className="lv-caption">Import progress is unavailable right now.</p>}
      {jobs.data && !jobs.data.length && <p className="lv-caption">No imports yet.</p>}
      {jobs.data?.map((job) => <Job job={job} settings={settings} refreshJobs={refreshJobs} key={job.job_id} />)}
    </div>
  );
}
