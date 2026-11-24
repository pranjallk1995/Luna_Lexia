import { useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Eye, KeyRound, Pencil, Plus, Trash2, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { passwords, useDocumentTypes, usePasswords, type RevealedPassword, type SavedPassword } from "../api";
import { messageOf } from "../lib";
import { Dialog } from "./Dialog";
import { useToast } from "./Toasts";

/** How long a shown password stays on screen before its dialog closes by itself. */
const SHOWN_SECONDS = 60;

function lastWorked(item: SavedPassword): string {
  if (!item.last_worked_at) return "Not used yet";
  const date = new Date(item.last_worked_at).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  return `Opened ${item.times_worked} PDF${item.times_worked === 1 ? "" : "s"}, last on ${date}`;
}

function DocTypeSelect({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { types } = useDocumentTypes();
  return (
    <select className="lv-select" aria-label="Document type" value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="any">Any locked PDF</option>
      {types.map((type) => (
        <option value={type.doc_type} key={type.doc_type}>
          {type.label}
        </option>
      ))}
    </select>
  );
}

function AddPassword({ onSaved }: { onSaved: () => void }) {
  const toast = useToast();
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
      <DocTypeSelect value={docType} onChange={setDocType} />
      <button className="lv-button lv-button-wide" type="submit" disabled={!ready || busy}>
        <Plus size={17} />
        {busy ? "Saving…" : "Save password"}
      </button>
    </form>
  );
}

type PinFormProps = {
  action: string;
  icon: LucideIcon;
  ready?: boolean;
  run: (pin: string) => Promise<unknown>;
  onCancel: () => void;
  children?: ReactNode;
};

/** Any fields of the change, then the Luna PIN that confirms it; stays open with the reason if it fails. */
function PinForm({ action, icon: Icon, ready = true, run, onCancel, children }: PinFormProps) {
  const [pin, setPin] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const confirm = async () => {
    setBusy(true);
    setError("");
    try {
      await run(pin);
    } catch (failure) {
      setError(messageOf(failure, "That did not work. Please try again."));
      setPin("");
      setBusy(false);
    }
  };

  return (
    <form
      className="lv-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (pin && ready && !busy) confirm();
      }}
    >
      {children}
      <input
        className="lv-input"
        type="password"
        inputMode="numeric"
        autoComplete="off"
        maxLength={12}
        aria-label="PIN"
        placeholder="Your Luna PIN, to confirm"
        value={pin}
        onChange={(event) => setPin(event.target.value.replace(/\D/g, ""))}
        autoFocus={!children}
      />
      {error && <p className="lv-alert lv-alert-error">{error}</p>}
      <div className="lv-confirm-actions">
        <button className="lv-button" type="button" onClick={onCancel}>
          Cancel
        </button>
        <button className="lv-button lv-button-primary" type="submit" disabled={!pin || !ready || busy}>
          <Icon size={17} />
          {busy ? "Checking…" : action}
        </button>
      </div>
    </form>
  );
}

function ShowDialog({ item, onClose }: { item: SavedPassword; onClose: () => void }) {
  const toast = useToast();
  const [shown, setShown] = useState<RevealedPassword | null>(null);
  const [copied, setCopied] = useState(false);

  // A shown password closes by itself after a minute, so it is not left on screen.
  useEffect(() => {
    if (!shown) return;
    const timer = window.setTimeout(onClose, SHOWN_SECONDS * 1000);
    return () => window.clearTimeout(timer);
  }, [shown, onClose]);

  const copy = async () => {
    if (!shown) return;
    try {
      await navigator.clipboard.writeText(shown.password);
      setCopied(true);
    } catch {
      toast("Could not copy the password.");
    }
  };

  return (
    <Dialog title="Show saved password" onClose={onClose}>
      {shown ? (
        <div className="lv-form">
          <p className="lv-confirm-text">
            The password for <strong>{shown.label}</strong>
          </p>
          <div className="lx-password-shown">
            <code>{shown.password}</code>
            <button className="lv-icon-button" type="button" aria-label="Copy password" onClick={copy}>
              {copied ? <Check size={16} /> : <Copy size={16} />}
            </button>
          </div>
          <p className="lv-caption">This closes by itself in a minute.</p>
          <button className="lv-button lv-button-wide" type="button" onClick={onClose} autoFocus>
            Done
          </button>
        </div>
      ) : (
        <>
          <p className="lv-confirm-text">
            Enter your PIN to see the password for <strong>{item.label}</strong>
          </p>
          <PinForm
            action="Show"
            icon={Eye}
            run={async (pin) => setShown(await passwords.reveal(item.password_id, pin))}
            onCancel={onClose}
          />
        </>
      )}
    </Dialog>
  );
}

function EditDialog({ item, onSaved, onClose }: { item: SavedPassword; onSaved: () => void; onClose: () => void }) {
  const toast = useToast();
  const [label, setLabel] = useState(item.label);
  const [password, setPassword] = useState("");
  const [docType, setDocType] = useState(item.doc_type ?? "any");

  return (
    <Dialog title="Edit saved password" onClose={onClose}>
      <PinForm
        action="Save changes"
        icon={Pencil}
        ready={Boolean(label.trim())}
        run={async (pin) => {
          await passwords.update(item.password_id, pin, {
            label: label.trim(),
            password,
            doc_type: docType === "any" ? null : docType,
          });
          toast(`Updated "${label.trim()}"`);
          onSaved();
          onClose();
        }}
        onCancel={onClose}
      >
        <input
          className="lv-input"
          autoComplete="off"
          aria-label="Label"
          placeholder="Label"
          value={label}
          onChange={(event) => setLabel(event.target.value)}
          autoFocus
        />
        <input
          className="lv-input"
          type="password"
          autoComplete="new-password"
          aria-label="New password"
          placeholder="New password (leave empty to keep the current one)"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        <DocTypeSelect value={docType} onChange={setDocType} />
      </PinForm>
    </Dialog>
  );
}

function DeleteDialog({ item, onDeleted, onClose }: { item: SavedPassword; onDeleted: () => void; onClose: () => void }) {
  return (
    <Dialog title="Delete saved password" onClose={onClose}>
      <p className="lv-confirm-text">
        Locked PDFs will no longer be tried with it. <strong>{item.label}</strong>
      </p>
      <PinForm
        action="Delete"
        icon={Trash2}
        run={async (pin) => {
          await passwords.forget(item.password_id, pin);
          onDeleted();
          onClose();
        }}
        onCancel={onClose}
      />
    </Dialog>
  );
}

type Opened = { mode: "show" | "edit" | "delete"; item: SavedPassword };

/** Passwords of locked PDFs, tried by themselves on every locked upload or import: those of its type first. */
export function PasswordsPanel() {
  const client = useQueryClient();
  const saved = usePasswords();
  const { label: typeLabel } = useDocumentTypes();
  const [opened, setOpened] = useState<Opened | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ["passwords"] });
  const close = useCallback(() => setOpened(null), []);

  return (
    <div className="lx-drive">
      <p className="lv-caption">
        A locked PDF you upload or import is tried with these, the ones for its document type first, so a statement
        locked with the same password each month opens by itself. They are stored encrypted; seeing, changing, or
        deleting one asks for your Luna PIN.
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
          <button className="lv-icon-button" type="button" aria-label={`Show ${item.label}`} onClick={() => setOpened({ mode: "show", item })}>
            <Eye size={16} />
          </button>
          <button className="lv-icon-button" type="button" aria-label={`Edit ${item.label}`} onClick={() => setOpened({ mode: "edit", item })}>
            <Pencil size={16} />
          </button>
          <button className="lv-icon-button" type="button" aria-label={`Delete ${item.label}`} onClick={() => setOpened({ mode: "delete", item })}>
            <Trash2 size={16} />
          </button>
        </div>
      ))}
      {opened?.mode === "show" && <ShowDialog item={opened.item} onClose={close} />}
      {opened?.mode === "edit" && <EditDialog item={opened.item} onSaved={refresh} onClose={close} />}
      {opened?.mode === "delete" && <DeleteDialog item={opened.item} onDeleted={refresh} onClose={close} />}
    </div>
  );
}
