import { useCallback, useEffect, useMemo, useState } from "react";
import { MessageSquareQuote, NotebookPen, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ApiClient } from "../api/client";
import type { ObservationSummary } from "../api/types";
import { useConfirmation } from "./DialogSystem";
import type { SelectionActionDraft } from "./selection";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

interface LinkOption {
  id: string;
  label: string;
}

interface NotesPanelProps {
  api: ApiClient;
  engagementId: string;
  evidenceOptions?: LinkOption[];
  assetOptions?: LinkOption[];
  initialDraft?: SelectionActionDraft;
  onInitialDraftConsumed?: () => void;
  onAskNebula?: (context: {
    text: string;
    sourceKind: "note";
    sourceId?: string;
    sourceLabel: string;
  }) => void;
}

const blank = {
  title: "",
  body: "",
  evidenceIds: [] as string[],
  assetIds: [] as string[],
};

export function NotesPanel({
  api,
  engagementId,
  evidenceOptions = [],
  assetOptions = [],
  initialDraft,
  onInitialDraftConsumed,
  onAskNebula,
}: NotesPanelProps) {
  const confirm = useConfirmation();
  const [notes, setNotes] = useState<ObservationSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string>();
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState(blank);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();

  const selected = useMemo(
    () => notes.find((note) => note.id === selectedId),
    [notes, selectedId],
  );

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      const response = await api.listObservations(engagementId, signal);
      const next = response.items.filter((item) => item.observationType === "note");
      setNotes(next);
      setSelectedId((current) => current && next.some((item) => item.id === current)
        ? current
        : next[0]?.id);
    } catch (loadError) {
      void logCaughtDiagnostic("interface.notes_panel.caught_failure_01", "A handled interface operation failed.", loadError, "notes_panel");
      if (!signal?.aborted) {
        setError(loadError instanceof Error ? loadError.message : "Could not load notes.");
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, engagementId]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (creating) return;
    if (!selected) {
      setDraft(blank);
      return;
    }
    setDraft({
      title: selected.title,
      body: selected.body,
      evidenceIds: selected.evidenceIds,
      assetIds: selected.assetIds,
    });
  }, [creating, selected]);

  useEffect(() => {
    if (!initialDraft) return;
    const label = initialDraft.source.label.trim() || "selected text";
    setCreating(true);
    setSelectedId(undefined);
    setDraft({
      title: `Note from ${label}`.slice(0, 500),
      body: initialDraft.text,
      evidenceIds: [],
      assetIds: [],
    });
    setError(undefined);
    onInitialDraftConsumed?.();
  }, [initialDraft, onInitialDraftConsumed]);

  const startNote = () => {
    setCreating(true);
    setSelectedId(undefined);
    setDraft(blank);
    setError(undefined);
  };

  const save = async () => {
    if (!draft.title.trim()) return;
    setSaving(true);
    setError(undefined);
    try {
      if (creating || !selected) {
        const created = await api.createObservation({
          engagementId,
          observationType: "note",
          title: draft.title,
          body: draft.body,
          evidenceIds: draft.evidenceIds,
          assetIds: draft.assetIds,
          source: "operator-note",
        });
        setNotes((current) => [created, ...current]);
        setSelectedId(created.id);
        setCreating(false);
      } else {
        const updated = await api.updateObservation(selected.id, {
          title: draft.title,
          body: draft.body,
          evidenceIds: draft.evidenceIds,
          assetIds: draft.assetIds,
          expectedRevision: selected.revision,
        });
        setNotes((current) => current.map((item) => item.id === updated.id ? updated : item));
      }
    } catch (saveError) {
      void logCaughtDiagnostic("interface.notes_panel.caught_failure_02", "A handled interface operation failed.", saveError, "notes_panel");
      setError(saveError instanceof Error ? saveError.message : "Could not save the note.");
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!selected) return;
    const approved = await confirm({
      title: "Delete this note?",
      message: "This removes the mutable note. Linked immutable evidence is retained.",
      confirmLabel: "Delete note",
      tone: "danger",
    });
    if (!approved) return;
    try {
      await api.deleteObservation(selected.id, selected.revision);
      const next = notes.filter((item) => item.id !== selected.id);
      setNotes(next);
      setSelectedId(next[0]?.id);
    } catch (deleteError) {
      void logCaughtDiagnostic("interface.notes_panel.caught_failure_03", "A handled interface operation failed.", deleteError, "notes_panel");
      setError(deleteError instanceof Error ? deleteError.message : "Could not delete the note.");
    }
  };

  const toggleLink = (field: "evidenceIds" | "assetIds", id: string) => {
    setDraft((current) => ({
      ...current,
      [field]: current[field].includes(id)
        ? current[field].filter((value) => value !== id)
        : [...current[field], id],
    }));
  };

  return (
    <div className="notes-panel">
      <aside className="notes-list" aria-label="Project notes">
        <header>
          <strong>Notes</strong>
          <div>
            <button className="button quiet square" type="button" aria-label="Refresh notes" disabled={loading} onClick={() => void load()}><RefreshCw className={loading ? "spin" : undefined} size={14} /></button>
            <button className="button quiet square" type="button" aria-label="Create note" onClick={startNote}><Plus size={15} /></button>
          </div>
        </header>
        {notes.map((note) => <button type="button" className={note.id === selectedId ? "active" : undefined} key={note.id} onClick={() => { setCreating(false); setSelectedId(note.id); }}><strong>{note.title}</strong><small>{new Date(note.updatedAt).toLocaleString()}</small></button>)}
        {!notes.length && !loading && <p>No notes yet.</p>}
      </aside>
      <section className={`note-editor${creating || selected ? "" : " is-empty"}`} aria-label={creating ? "New note" : selected ? `Edit ${selected.title}` : "Note editor"}>
        {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
        {(creating || selected) ? <>
          <header>
            <input aria-label="Note title" value={draft.title} placeholder="Note title" maxLength={500} onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))} />
            <div>
              {selected && <button className="button quiet" type="button" onClick={() => void remove()}><Trash2 size={14} /> Delete</button>}
              <button className="button quiet" type="button" disabled={!draft.body.trim() || !onAskNebula} onClick={() => onAskNebula?.({ text: draft.body, sourceKind: "note", sourceId: selected?.id, sourceLabel: draft.title || "Untitled note" })}><MessageSquareQuote size={14} /> Ask Nebula</button>
              <button className="button primary" type="button" disabled={saving || !draft.title.trim()} onClick={() => void save()}><Save size={14} /> {saving ? "Saving…" : "Save"}</button>
            </div>
          </header>
          <div className="note-editor-body">
            <label>Markdown<textarea aria-label="Note body" rows={18} value={draft.body} placeholder="Capture observations, test ideas, or conclusions…" onChange={(event) => setDraft((current) => ({ ...current, body: event.target.value }))} /></label>
            <section className="note-preview" aria-label="Note preview"><ReactMarkdown remarkPlugins={[remarkGfm]}>{draft.body || "_Nothing to preview yet._"}</ReactMarkdown></section>
          </div>
          {(evidenceOptions.length > 0 || assetOptions.length > 0) && <details><summary>Links · {draft.evidenceIds.length} evidence · {draft.assetIds.length} assets</summary>
            {evidenceOptions.map((option) => <label key={option.id}><input type="checkbox" checked={draft.evidenceIds.includes(option.id)} onChange={() => toggleLink("evidenceIds", option.id)} /> {option.label}</label>)}
            {assetOptions.map((option) => <label key={option.id}><input type="checkbox" checked={draft.assetIds.includes(option.id)} onChange={() => toggleLink("assetIds", option.id)} /> {option.label}</label>)}
          </details>}
        </> : <div className="empty-state note-empty-state"><NotebookPen size={24} aria-hidden="true" /><strong>Start a project note</strong><p>Capture working thoughts in Markdown. Preserve exact files and screenshots as Evidence.</p><button className="button primary" type="button" onClick={startNote}><Plus size={14} /> New note</button></div>}
      </section>
    </div>
  );
}
