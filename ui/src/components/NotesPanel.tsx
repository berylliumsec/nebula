import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MessageSquareQuote, NotebookPen, Plus, RefreshCw, Save, Sparkles, Trash2 } from "lucide-react";
import type { ApiClient } from "../api/client";
import type {
  ObservationCreateRequest,
  ObservationSummary,
  ObservationUpdateRequest,
  ProviderHealth,
} from "../api/types";
import { useConfirmation } from "./DialogSystem";
import { AIWritingDialog } from "./AIWritingDialog";
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
  providers?: ProviderHealth[];
  initialDraft?: SelectionActionDraft;
  onInitialDraftConsumed?: () => void;
  createObservation?: (request: ObservationCreateRequest) => Promise<ObservationSummary>;
  updateObservation?: (id: string, request: ObservationUpdateRequest) => Promise<ObservationSummary>;
  deleteObservation?: (id: string, expectedRevision: number) => Promise<void>;
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
  metadata: {} as Record<string, unknown>,
};

export function NotesPanel({
  api,
  engagementId,
  evidenceOptions = [],
  assetOptions = [],
  providers = [],
  initialDraft,
  onInitialDraftConsumed,
  createObservation,
  updateObservation,
  deleteObservation,
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
  const [writingOpen, setWritingOpen] = useState(false);
  const consumedInitialDraftRef = useRef<SelectionActionDraft | undefined>(undefined);
  const capturedNoteRef = useRef<ObservationSummary | undefined>(undefined);
  const createNote = useCallback(
    (request: ObservationCreateRequest) => createObservation ? createObservation(request) : api.createObservation(request),
    [api, createObservation],
  );
  const updateNote = useCallback(
    (id: string, request: ObservationUpdateRequest) => updateObservation ? updateObservation(id, request) : api.updateObservation(id, request),
    [api, updateObservation],
  );
  const deleteNote = useCallback(
    (id: string, expectedRevision: number) => deleteObservation ? deleteObservation(id, expectedRevision) : api.deleteObservation(id, expectedRevision),
    [api, deleteObservation],
  );

  const selected = useMemo(
    () => notes.find((note) => note.id === selectedId),
    [notes, selectedId],
  );

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      const response = await api.listObservations(engagementId, signal);
      const next = response.items.filter((item) => item.observationType === "note" || item.observationType === "ai_tool_note");
      const captured = capturedNoteRef.current;
      const merged = captured && !next.some((item) => item.id === captured.id) ? [captured, ...next] : next;
      setNotes(merged);
      setSelectedId((current) => current && merged.some((item) => item.id === current)
        ? current
        : merged[0]?.id);
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
    if (creating || initialDraft) return;
    if (!selected) {
      setDraft(blank);
      return;
    }
    setDraft({
      title: selected.title,
      body: selected.body,
      evidenceIds: selected.evidenceIds,
      assetIds: selected.assetIds,
      metadata: selected.metadata,
    });
  }, [creating, initialDraft, selected]);

  useEffect(() => {
    if (!initialDraft || consumedInitialDraftRef.current === initialDraft) return;
    consumedInitialDraftRef.current = initialDraft;
    const label = initialDraft.source.label.trim() || "selected text";
    setCreating(true);
    setSelectedId(undefined);
    const nextDraft = {
      title: `Note from ${label}`.slice(0, 500),
      body: initialDraft.text,
      evidenceIds: [],
      assetIds: [],
      metadata: {
        selection_source: {
          kind: initialDraft.source.kind,
          id: initialDraft.source.id,
          label: initialDraft.source.label,
          truncated: initialDraft.truncated,
          original_length: initialDraft.originalLength,
        },
      },
    };
    setDraft(nextDraft);
    setError(undefined);
    onInitialDraftConsumed?.();
    setSaving(true);
    void createNote({
      engagementId,
      observationType: "note",
      title: nextDraft.title,
      body: nextDraft.body,
      source: "selection-note",
      metadata: nextDraft.metadata,
    }).then((created) => {
      capturedNoteRef.current = created;
      setNotes((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setSelectedId(created.id);
      setCreating(false);
    }).catch((saveError: unknown) => {
      void logCaughtDiagnostic("interface.notes_panel.selection_capture_failed", "A selected-text note could not be saved.", saveError, "notes_panel");
      setError(saveError instanceof Error ? saveError.message : "Could not save the selected text as a note.");
    }).finally(() => setSaving(false));
  }, [createNote, engagementId, initialDraft, onInitialDraftConsumed]);

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
        const created = await createNote({
          engagementId,
          observationType: "note",
          title: draft.title,
          body: draft.body,
          evidenceIds: draft.evidenceIds,
          assetIds: draft.assetIds,
          source: "operator-note",
          metadata: draft.metadata,
        });
        setNotes((current) => [created, ...current]);
        setSelectedId(created.id);
        setCreating(false);
      } else {
        const updated = await updateNote(selected.id, {
          title: draft.title,
          body: draft.body,
          evidenceIds: draft.evidenceIds,
          assetIds: draft.assetIds,
          metadata: draft.metadata,
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
      await deleteNote(selected.id, selected.revision);
      if (capturedNoteRef.current?.id === selected.id) capturedNoteRef.current = undefined;
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
        {notes.map((note) => <button type="button" className={note.id === selectedId ? "active" : undefined} key={note.id} onClick={() => { setCreating(false); setSelectedId(note.id); }}><strong>{note.title}</strong><small>{note.observationType === "ai_tool_note" ? "AI-generated · " : ""}{new Date(note.updatedAt).toLocaleString()}</small></button>)}
        {!notes.length && !loading && <p>No notes yet.</p>}
      </aside>
      <section className={`note-editor${creating || selected ? "" : " is-empty"}`} aria-label={creating ? "New note" : selected ? `Edit ${selected.title}` : "Note editor"}>
        {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
        {(creating || selected) ? <>
          <header>
            <input aria-label="Note title" value={draft.title} placeholder="Note title" maxLength={500} onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))} />
            <div>
              {selected && <button className="button quiet" type="button" onClick={() => void remove()}><Trash2 size={14} /> Delete</button>}
              <button className="button quiet" type="button" disabled={!draft.body.trim() || !providers.some((provider) => provider.enabled && provider.models.length)} onClick={() => setWritingOpen(true)}><Sparkles size={14} /> Transform with AI</button>
              <button className="button quiet" type="button" disabled={!draft.body.trim() || !onAskNebula} onClick={() => onAskNebula?.({ text: draft.body, sourceKind: "note", sourceId: selected?.id, sourceLabel: draft.title || "Untitled note" })}><MessageSquareQuote size={14} /> Ask Nebula</button>
              <button className="button primary" type="button" disabled={saving || !draft.title.trim()} onClick={() => void save()}><Save size={14} /> {saving ? "Saving…" : "Save"}</button>
            </div>
          </header>
          <div className="note-editor-body">
            <label>Markdown<textarea aria-label="Note body" rows={18} value={draft.body} placeholder="Capture observations, test ideas, or conclusions…" onChange={(event) => setDraft((current) => ({ ...current, body: event.target.value }))} /></label>
          </div>
          {(evidenceOptions.length > 0 || assetOptions.length > 0) && <details><summary>Links · {draft.evidenceIds.length} evidence · {draft.assetIds.length} assets</summary>
            {evidenceOptions.map((option) => <label key={option.id}><input type="checkbox" checked={draft.evidenceIds.includes(option.id)} onChange={() => toggleLink("evidenceIds", option.id)} /> {option.label}</label>)}
            {assetOptions.map((option) => <label key={option.id}><input type="checkbox" checked={draft.assetIds.includes(option.id)} onChange={() => toggleLink("assetIds", option.id)} /> {option.label}</label>)}
          </details>}
        </> : <div className="empty-state note-empty-state"><NotebookPen size={24} aria-hidden="true" /><strong>Start a project note</strong><p>Capture working thoughts in Markdown. Preserve exact files and screenshots as Evidence.</p><button className="button primary" type="button" onClick={startNote}><Plus size={14} /> New note</button></div>}
      </section>
      {writingOpen && <AIWritingDialog
        api={api}
        engagementId={engagementId}
        providers={providers}
        purpose="note"
        title="Transform note with AI"
        description="Tell Nebula how to organize or rewrite this note. The generated text remains editable and is not persisted until you save the note."
        sourceLabel={draft.title || "Untitled note"}
        sourceText={draft.body}
        initialInstruction="Turn this into a concise analyst note. Preserve exact observations, separate hypotheses, and keep useful technical details."
        onClose={() => setWritingOpen(false)}
        onApply={(result) => {
          setDraft((current) => ({
            ...current,
            body: result.content,
            metadata: { ...current.metadata, ai_writing: result.provenance },
          }));
          setWritingOpen(false);
        }}
      />}
    </div>
  );
}
