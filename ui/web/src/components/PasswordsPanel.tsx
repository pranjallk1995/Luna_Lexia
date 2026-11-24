import { useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { passwords, useDocumentTypes, usePasswords, type SavedPassword } from "../api";
import { messageOf } from "../lib";
import { ConfirmDialog } from "./ConfirmDialog";
import { useToast } from "./Toasts";

function lastWorked(item: SavedPassword): string {
  if (!item.last_worked_at) return "Not used yet";
  const date = new Date(item.last_worked_at).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  return `Opened ${item.times_worked} PDF${item.times_worked === 1 ? "" : "s"}, last on ${date}`;
}

function AddPassword({ onSaved }: { onSaved: () => void }) {
  const toast = useToast();
  const { types } = useDocumentTypes();
  const [label, setLabel] = useState("");
  const [password, setPassword] = useState("");
  const [docType, setDocType] = useState("any");
  const [busy, setBusy] = useState(false);
  const ready = label.trim() && password;

  const save = async () => {
    setBusy(true);
    try {
      await passwords.save(label.trim(), password, docType === "any" ? null : docType);
      toast(`Saved "${label.trim()}"`);
      setLabel("");
      setPassword("");
      setDocType("any");
      onSaved();
    } catch (failure) {
      toast(messageOf(failure, "Could not save the password."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="lv-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready && !busy) save();
      }}
    >
      <input
        className="lv-input"
        autoComplete="off"
        aria-label="Label"
        placeholder="Label, e.g. HDFC card statement"
        value={label}
        onChange={(event) => setLabel(event.target.value)}
      />
      <input
        className="lv-input"
        type="password"
        autoComplete="new-password"
        aria-label="Password"
        placeholder="Its PDF password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
      />
      <select className="lv-select" aria-label="Document type" value={docType} onChange={(event) => setDocType(event.target.value)}>
        <option value="any">Any locked PDF</option>
        {types.map((type) => (
          <option value={type.doc_type} key={type.doc_type}>
            {type.label}
          </option>
        ))}
      </select>
      <button className="lv-button lv-button-wide" type="submit" disabled={!ready || busy}>
        <Plus size={17} />
        {busy ? "Saving…" : "Save password"}
      </button>
    </form>
  );
}

/** Passwords of locked PDFs, tried by themselves on every locked upload or import: those of its type first. */
export function PasswordsPanel() {
  const client = useQueryClient();
  const saved = usePasswords();
  const { label: typeLabel } = useDocumentTypes();
  const [forgetting, setForgetting] = useState<SavedPassword | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ["passwords"] });

  return (
    <div className="lx-drive">
      <p className="lv-caption">
        A locked PDF you upload or import is tried with these, the ones for its document type first, so a statement
        locked with the same password each month opens by itself. They are stored encrypted and never shown again.
      </p>
      <AddPassword onSaved={refresh} />
      <h3 className="lx-jobs-title">Saved</h3>
      {saved.error && <p className="lv-alert lv-alert-error">{messageOf(saved.error, "Could not load the saved passwords.")}</p>}
      {saved.data && !saved.data.length && <p className="lv-caption">No passwords saved yet.</p>}
      {saved.data?.map((item) => (
        <div className="lx-reextract lx-password" key={item.password_id}>
          <KeyRound size={16} />
          <span className="lx-split-label">
            {item.label}
            <small>
              {item.doc_type ? typeLabel(item.doc_type) : "Any locked PDF"} · {lastWorked(item)}
            </small>
          </span>
          <button className="lv-icon-button" type="button" aria-label={`Delete ${item.label}`} onClick={() => setForgetting(item)}>
            <Trash2 size={16} />
          </button>
        </div>
      ))}
      {forgetting && (
        <ConfirmDialog
          title="Delete saved password"
          message="Locked PDFs will no longer be tried with it."
          subject={forgetting.label}
          action="Delete"
          icon={Trash2}
          run={async () => {
            await passwords.forget(forgetting.password_id);
            refresh();
          }}
          onClose={() => setForgetting(null)}
        />
      )}
    </div>
  );
}
