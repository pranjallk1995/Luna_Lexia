import { Check, X } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import type { DocumentRecord, NamedMetadataChange, Settings } from "../api";
import { bulkChanges, itemCount, messageOf, parsedTags, tagCounts, tagSlot, type CaptionEdit } from "../lib";
import { Dialog } from "./Dialog";

type CaptionMode = CaptionEdit["mode"];

const CAPTION_MODES: { mode: CaptionMode; label: string }[] = [
  { mode: "keep", label: "Keep as they are" },
  { mode: "replace", label: "Find and replace text" },
  { mode: "set", label: "Give them all one summary" },
];

type Props = {
  documents: DocumentRecord[];
  settings: Settings;
  run: (changes: NamedMetadataChange[]) => Promise<unknown>;
  onClose: () => void;
};

/** Correct the tags and descriptions of many documents together, seeing how many it will change. */
export function BulkEditDialog({ documents, settings, run, onClose }: Props) {
  const [addText, setAddText] = useState("");
  const [removeTags, setRemoveTags] = useState<string[]>([]);
  const [mode, setMode] = useState<CaptionMode>("keep");
  const [text, setText] = useState("");
  const [find, setFind] = useState("");
  const [replacement, setReplacement] = useState("");
  const [markReviewed, setMarkReviewed] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const existing = useMemo(() => tagCounts(documents), [documents]);
  const changes = useMemo(() => {
    const caption: CaptionEdit =
      mode === "set" ? { mode, text } : mode === "replace" ? { mode, find, replacement } : { mode };
    return bulkChanges(documents, { addTags: parsedTags(addText), removeTags, caption, markReviewed });
  }, [documents, addText, removeTags, mode, text, find, replacement, markReviewed]);

  const toggleRemoval = (tag: string) =>
    setRemoveTags((current) => (current.includes(tag) ? current.filter((item) => item !== tag) : [...current, tag]));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await run(changes);
      onClose();
    } catch (failure) {
      setError(messageOf(failure, "The documents could not be updated."));
      setBusy(false);
    }
  };

  return (
    <Dialog title={`Edit ${itemCount(documents.length)}`} onClose={onClose}>
      <form className="lv-form" onSubmit={submit}>
        <label className="lv-field">
          <span>Add tags, separated by commas</span>
          <input
            className="lv-input"
            autoFocus
            placeholder="holiday, 2026"
            value={addText}
            onChange={(event) => setAddText(event.target.value)}
          />
        </label>

        {existing.length > 0 && (
          <div className="lv-field">
            <span id="lv-remove-tags">Remove tags (the number is how many selected documents have it)</span>
            <div className="lv-bulk-tags" role="group" aria-labelledby="lv-remove-tags">
              {existing.map(({ tag, count }) => {
                const removing = removeTags.includes(tag);
                return (
                  <button
                    className={`lv-tag ${removing ? "lv-tag-removing" : `lv-tag-${tagSlot(tag)}`}`}
                    type="button"
                    key={tag}
                    aria-pressed={removing}
                    onClick={() => toggleRemoval(tag)}
                  >
                    {removing && <X size={12} />}
                    {tag} · {count}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        <label className="lv-field">
          <span>Summaries</span>
          <select className="lv-select" value={mode} onChange={(event) => setMode(event.target.value as CaptionMode)}>
            {CAPTION_MODES.map((option) => (
              <option key={option.mode} value={option.mode}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        {mode === "set" && (
          <label className="lv-field">
            <span>New summary for every selected document</span>
            <input
              className="lv-input"
              maxLength={settings.summary_max_chars}
              value={text}
              onChange={(event) => setText(event.target.value)}
            />
          </label>
        )}

        {mode === "replace" && (
          <div className="lv-form-row">
            <label className="lv-field">
              <span>Find (any capitalization)</span>
              <input className="lv-input" value={find} onChange={(event) => setFind(event.target.value)} />
            </label>
            <label className="lv-field">
              <span>Replace with</span>
              <input className="lv-input" value={replacement} onChange={(event) => setReplacement(event.target.value)} />
            </label>
          </div>
        )}

        {documents.some((document) => document.review_flags?.length) && (
          <label className="lv-check-field">
            <input type="checkbox" checked={markReviewed} onChange={(event) => setMarkReviewed(event.target.checked)} />
            <span>Mark the ones that need review as reviewed</span>
          </label>
        )}

        {error && <p className="lv-alert lv-alert-error">{error}</p>}
        <p className="lv-caption" role="status">
          This changes {changes.length} of {itemCount(documents.length)}.
        </p>

        <div className="lv-confirm-actions">
          <button className="lv-button" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="lv-button lv-button-primary" type="submit" disabled={busy || !changes.length}>
            <Check size={17} />
            Apply
          </button>
        </div>
      </form>
    </Dialog>
  );
}
