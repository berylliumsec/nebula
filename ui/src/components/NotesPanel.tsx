import { IconAction } from "./IconAction";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, MessageSquareQuote, NotebookPen, Plus, RefreshCw, Save, Sparkles, Trash2 } from "lucide-react";
import type { ApiClient } from "../api/client";
import type {
  ObservationCreateRequest,
  ObservationSummary,
  ObservationUpdateRequest,
  HarnessProfile,
  ProviderHealth,
  ObservationDependencies,
} from "../api/types";
import { ModalSurface, useConfirmation } from "./DialogSystem";
import { AIWritingDialog } from "./AIWritingDialog";
import type { SelectionActionDraft } from "./selection";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { aiRuntimeOptions } from "./aiRuntimes";
import { LoadingSurface, StandardEmptyState } from "./SurfacePrimitives";
import { randomId } from "../randomId";

interface LinkOption {
  id: string;
  label: string;
}

interface NotesPanelProps {
  api: ApiClient;
  engagementId: string;
  initialNotes?: ObservationSummary[];
  initialNotesReady?: boolean;
  initialNotesLoading?: boolean;
  panelSessionKey?: string;

  evidenceOptions?: LinkOption[];
  assetOptions?: LinkOption[];
  linksLoading?: boolean;
  providers?: ProviderHealth[];
  harnesses?: HarnessProfile[];
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
const LINK_RESULT_LIMIT = 40;
const CAPTURE_RESULT_LIMIT = 40;
const CAPTURE_HISTORY_LIMIT = 16;

function appendedNoteBody(body: string, selection: string) {
  return body.trim() ? `${body.trimEnd()}\n\n${selection}` : selection;
}

function captureIds(note: ObservationSummary) {
  const ids = note.metadata.selection_capture_ids;
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string").slice(-CAPTURE_HISTORY_LIMIT) : [];
}

function matchingLinkOptions(options: LinkOption[], query: string, selectedIds: string[]) {
  const selected = new Set(selectedIds);
  const selectedMatches: LinkOption[] = [];
  const otherMatches: LinkOption[] = [];
  let total = 0;
  for (const option of options) {
    if (query && !option.label.toLowerCase().includes(query)) continue;
    total += 1;
    if (selected.has(option.id)) selectedMatches.push(option);
    else if (otherMatches.length < LINK_RESULT_LIMIT) otherMatches.push(option);
  }
  return { total, items: [...selectedMatches, ...otherMatches].slice(0, LINK_RESULT_LIMIT) };
}

const NoteBodyEditor = memo(function NoteBodyEditor({ body, onChange }: { body: string; onChange: (body: string) => void }) {
  const [value, setValue] = useState(body);
  useEffect(() => setValue(body), [body]);
  return <textarea aria-label="Note body" rows={18} value={value} placeholder="Capture observations, test ideas, or conclusions…" onChange={(event) => {
    setValue(event.target.value);
    onChange(event.target.value);
  }} />;
});
interface NotePanelState {
  selectedId?: string;
  creating: boolean;
  draft: typeof blank;
  syncedNoteKey?: string;
}

// Saved notes live in Core for the project. Draft and selection stay with a chat.
const notePanelStates = new Map<string, NotePanelState>();

export function adoptNotePanelState(from: string, to: string) {
  if (from === to || !notePanelStates.has(from) || notePanelStates.has(to)) return;
  notePanelStates.set(to, notePanelStates.get(from)!);
  notePanelStates.delete(from);
}


export function NotesPanel({
  api,
  engagementId,
  initialNotes = [],
  initialNotesReady = false,
  initialNotesLoading = false,
  panelSessionKey,

  evidenceOptions = [],
  assetOptions = [],
  linksLoading = false,
  providers = [],
  harnesses = [],
  initialDraft,
  onInitialDraftConsumed,
  createObservation,
  updateObservation,
  deleteObservation,
  onAskNebula,
}: NotesPanelProps) {
  const confirm = useConfirmation();
  const cachedNotesReady = initialNotesReady;
  const [notes, setNotes] = useState<ObservationSummary[]>(() => cachedNotesReady
    ? initialNotes.filter((note) => note.engagementId === engagementId && (note.observationType === "note" || note.observationType === "ai_tool_note"))
    : []);
  const restoredPanel = panelSessionKey ? notePanelStates.get(panelSessionKey) : undefined;
  const [selectedId, setSelectedId] = useState<string | undefined>(restoredPanel?.selectedId ?? notes[0]?.id);
  const [creating, setCreating] = useState(restoredPanel?.creating ?? false);
  const [draft, setDraft] = useState(restoredPanel?.draft ?? (notes[0] ? {
    title: notes[0].title, body: notes[0].body, evidenceIds: notes[0].evidenceIds,
    assetIds: notes[0].assetIds, metadata: notes[0].metadata,
  } : blank));
  const draftBodyRef = useRef(draft.body);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const savedSnapshotRef = useRef(JSON.stringify(draft));
  const autosaveTimerRef = useRef<number | undefined>(undefined);
  const saveRef = useRef<() => Promise<boolean>>(async () => false);
  const [saveStatus, setSaveStatus] = useState<"saved" | "unsaved" | "saving" | "failed">("saved");
  const queueAutosave = useCallback(() => {
    window.clearTimeout(autosaveTimerRef.current);
    setSaveStatus("unsaved");
    autosaveTimerRef.current = window.setTimeout(() => void saveRef.current(), 700);
  }, []);
  const [hasBody, setHasBody] = useState(Boolean(draft.body.trim()));
  const onBodyChange = useCallback((body: string) => {
    draftBodyRef.current = body;
    if (panelSessionKey) {
      const panel = notePanelStates.get(panelSessionKey);
      if (panel) notePanelStates.set(panelSessionKey, { ...panel, draft: { ...panel.draft, body } });
    }
    setHasBody((current) => current === Boolean(body.trim()) ? current : Boolean(body.trim()));
    queueAutosave();
  }, [panelSessionKey, queueAutosave]);
  const [loading, setLoading] = useState(!cachedNotesReady);

  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [error, setError] = useState<string>();
  const [writingOpen, setWritingOpen] = useState(false);
  const [linksOpen, setLinksOpen] = useState(false);
  const [linkQuery, setLinkQuery] = useState("");
  const [deleteBlocker, setDeleteBlocker] = useState<ObservationDependencies>();
  const consumedInitialDraftRef = useRef<SelectionActionDraft | undefined>(undefined);
  const [captureDraft, setCaptureDraft] = useState<SelectionActionDraft>();
  const [captureTargetId, setCaptureTargetId] = useState("");
  const [captureTitle, setCaptureTitle] = useState("");
  const [captureQuery, setCaptureQuery] = useState("");
  const [captureSaving, setCaptureSaving] = useState(false);
  const captureSavingRef = useRef(false);
  const captureIdRef = useRef("");
  const [captureError, setCaptureError] = useState<string>();
  const capturedNoteRef = useRef<ObservationSummary | undefined>(undefined);
  const creatingRef = useRef(false);
  creatingRef.current = creating;
  const syncedNoteRef = useRef<string | undefined>(restoredPanel?.syncedNoteKey);
  useEffect(() => () => {
    if (autosaveTimerRef.current !== undefined) {
      window.clearTimeout(autosaveTimerRef.current);
      void saveRef.current();
    }
  }, []);
  useEffect(() => {
    if (!panelSessionKey) return;
    if (!notePanelStates.has(panelSessionKey) && notePanelStates.size >= 64) notePanelStates.delete(notePanelStates.keys().next().value!);
    notePanelStates.set(panelSessionKey, { selectedId, creating, draft: { ...draft, body: draftBodyRef.current }, syncedNoteKey: syncedNoteRef.current });
  }, [panelSessionKey, selectedId, creating, draft]);
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
  const writingRuntimes = useMemo(
    () => aiRuntimeOptions(providers, harnesses),
    [harnesses, providers],
  );
  const normalizedLinkQuery = linkQuery.trim().toLowerCase();
  const matchingEvidence = useMemo(() => linksOpen
    ? matchingLinkOptions(evidenceOptions, normalizedLinkQuery, draft.evidenceIds)
    : { total: 0, items: [] }, [linksOpen, evidenceOptions, normalizedLinkQuery, draft.evidenceIds]);
  const matchingAssets = useMemo(() => linksOpen
    ? matchingLinkOptions(assetOptions, normalizedLinkQuery, draft.assetIds)
    : { total: 0, items: [] }, [linksOpen, assetOptions, normalizedLinkQuery, draft.assetIds]);

  useEffect(() => setDeleteBlocker(undefined), [selectedId]);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      const response = await api.listObservations(engagementId, signal);
      const next = response.items.filter((item) => item.observationType === "note" || item.observationType === "ai_tool_note");
      const captured = capturedNoteRef.current;
      const merged = captured && !next.some((item) => item.id === captured.id) ? [captured, ...next] : next;
      setNotes(merged);
      // A reload must not steal the selection from a note that is being created.
      setSelectedId((current) => creatingRef.current || (current && merged.some((item) => item.id === current))
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
    if (initialNotesReady || initialNotesLoading) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [initialNotesReady, initialNotesLoading, load]);

  useEffect(() => {
    if (!cachedNotesReady) return;
    const next = initialNotes.filter((note) => note.engagementId === engagementId && (note.observationType === "note" || note.observationType === "ai_tool_note"));
    setNotes(next);
    setSelectedId((current) => creatingRef.current || (current && next.some((note) => note.id === current)) ? current : next[0]?.id);
    setLoading(false);
  }, [cachedNotesReady, initialNotes, engagementId]);

  useEffect(() => {
    if (creating) {
      syncedNoteRef.current = undefined;
      return;
    }
    if (!selected) {
      syncedNoteRef.current = undefined;
      draftBodyRef.current = "";
      setHasBody(false);
      setDraft(blank);
      return;
    }
    // A reload returns a new object for the same saved note; only a different
    // note or a new revision may replace the operator's unsaved edits.
    const key = `${selected.id}:${selected.revision}`;
    if (syncedNoteRef.current === key) return;
    syncedNoteRef.current = key;
    draftBodyRef.current = selected.body;
    savedSnapshotRef.current = JSON.stringify({ title: selected.title, body: selected.body, evidenceIds: selected.evidenceIds, assetIds: selected.assetIds, metadata: selected.metadata });
    setSaveStatus("saved");
    setHasBody(Boolean(selected.body.trim()));
    setDraft({
      title: selected.title,
      body: selected.body,
      evidenceIds: selected.evidenceIds,
      assetIds: selected.assetIds,
      metadata: selected.metadata,
    });
  }, [creating, selected]);

  useEffect(() => {
    if (!initialDraft || consumedInitialDraftRef.current === initialDraft) return;
    consumedInitialDraftRef.current = initialDraft;
    captureIdRef.current = randomId();
    const label = initialDraft.source.label.trim() || "selected text";
    setCaptureDraft(initialDraft);
    setCaptureTargetId("");
    setCaptureTitle(`Note from ${label}`.slice(0, 500));
    setCaptureQuery("");
    setCaptureError(undefined);
  }, [initialDraft]);

  const closeCapture = () => {
    if (captureSavingRef.current) return;
    setCaptureDraft(undefined);
    setCaptureError(undefined);
    onInitialDraftConsumed?.();
  };

  const saveCapture = async () => {
    if (!captureDraft || !captureTargetId || captureSavingRef.current || (captureTargetId === "new" && !captureTitle.trim())) return;
    captureSavingRef.current = true;
    setCaptureSaving(true);
    setCaptureError(undefined);
    try {
      if (saveStatus === "unsaved" || saveStatus === "failed" || savingRef.current) {
        if (savingRef.current || !(await saveRef.current())) throw new Error("Save the current note before adding selected text. Retry after its save completes.");
      }
      let saved: ObservationSummary;
      const currentNotes = (await api.listObservations(engagementId)).items;
      const alreadySaved = currentNotes.find((item) => captureIds(item).includes(captureIdRef.current));
      if (alreadySaved) {
        saved = alreadySaved;
      } else if (captureTargetId === "new") {
        saved = await createNote({
          engagementId,
          observationType: "note",
          title: captureTitle,
          body: captureDraft.text,
          source: "selection-note",
          metadata: { selection_source: {
            kind: captureDraft.source.kind,
            id: captureDraft.source.id,
            label: captureDraft.source.label,
            truncated: captureDraft.truncated,
            original_length: captureDraft.originalLength,
          }, selection_capture_ids: [captureIdRef.current] },
        });
      } else {
        // Read the current revision and body before appending. The list visible
        // in this panel can be older than edits from another device.
        const current = currentNotes.find((item) => item.id === captureTargetId);
        if (!current || (current.observationType !== "note" && current.observationType !== "ai_tool_note")) {
          throw new Error("This note is no longer available. Refresh the list and choose another note.");
        }
        saved = await updateNote(current.id, {
          body: appendedNoteBody(current.body, captureDraft.text),
          metadata: { ...current.metadata, selection_capture_ids: [...captureIds(current).slice(-(CAPTURE_HISTORY_LIMIT - 1)), captureIdRef.current] },
          expectedRevision: current.revision,
        });
      }
      capturedNoteRef.current = saved;
      setNotes((current) => [saved, ...current.filter((item) => item.id !== saved.id)]);
      setCreating(false);
      setSelectedId(saved.id);
      setCaptureDraft(undefined);
      onInitialDraftConsumed?.();
    } catch (saveError) {
      void logCaughtDiagnostic("interface.notes_panel.selection_capture_failed", "Selected text could not be added to a note.", saveError, "notes_panel");
      setCaptureError(saveError instanceof Error ? saveError.message : "Could not add the selected text. Retry or choose another note.");
    } finally {
      captureSavingRef.current = false;
      setCaptureSaving(false);
    }
  };

  const startNote = async () => {
    if (savingRef.current || ((saveStatus === "unsaved" || saveStatus === "failed") && !(await saveRef.current()))) return;
    if (JSON.stringify({ ...draftRef.current, body: draftBodyRef.current }) !== savedSnapshotRef.current) return;
    window.clearTimeout(autosaveTimerRef.current);
    autosaveTimerRef.current = undefined;
    creatingRef.current = true;
    setCreating(true);
    setSelectedId(undefined);
    setDraft(blank);
    draftBodyRef.current = "";
    savedSnapshotRef.current = JSON.stringify(blank);
    setSaveStatus("saved");
    setHasBody(false);
    setError(undefined);
  };

  const save = async (): Promise<boolean> => {
    window.clearTimeout(autosaveTimerRef.current);
    autosaveTimerRef.current = undefined;
    const current = draftRef.current;
    if (!current.title.trim() || savingRef.current) return false;
    const bodyAtSave = draftBodyRef.current;
    const snapshot = JSON.stringify({ ...current, body: bodyAtSave });
    if (snapshot === savedSnapshotRef.current && saveStatus !== "failed") return true;
    savingRef.current = true;
    setSaving(true);
    setSaveStatus("saving");
    setError(undefined);
    try {
      if (creating || !selected) {
        const created = await createNote({
          engagementId,
          observationType: "note",
          title: current.title,
          body: bodyAtSave,
          evidenceIds: current.evidenceIds,
          assetIds: current.assetIds,
          source: "operator-note",
          metadata: current.metadata,
        });
        syncedNoteRef.current = `${created.id}:${created.revision}`;
        setDraft((latest) => ({ ...latest, body: draftBodyRef.current }));
        setNotes((current) => [created, ...current]);
        setSelectedId(created.id);
        setCreating(false);
      } else {
        const updated = await updateNote(selected.id, {
          title: current.title,
          body: bodyAtSave,
          evidenceIds: current.evidenceIds,
          assetIds: current.assetIds,
          metadata: current.metadata,
          expectedRevision: selected.revision,
        });
        syncedNoteRef.current = `${updated.id}:${updated.revision}`;
        setNotes((current) => current.map((item) => item.id === updated.id ? updated : item));
      }
      savedSnapshotRef.current = snapshot;
      if (JSON.stringify({ ...draftRef.current, body: draftBodyRef.current }) === snapshot) setSaveStatus("saved");
      else queueAutosave();
      return true;
    } catch (saveError) {
      void logCaughtDiagnostic("interface.notes_panel.caught_failure_02", "A handled interface operation failed.", saveError, "notes_panel");
      setError(saveError instanceof Error ? saveError.message : "Could not save the note.");
      setSaveStatus("failed");
      return false;
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };
  saveRef.current = save;

  const selectNote = async (id: string) => {
    if (savingRef.current) return;
    if ((saveStatus === "unsaved" || saveStatus === "failed") && !(await save())) return;
    if (JSON.stringify({ ...draftRef.current, body: draftBodyRef.current }) !== savedSnapshotRef.current) return;
    creatingRef.current = false;
    setCreating(false);
    setSelectedId(id);
  };

  const remove = async () => {
    if (!selected) return;
    setError(undefined);
    setDeleteBlocker(undefined);
    try {
      const dependencies = await api.observationDependencies(selected.id);
      if (!dependencies.deletable) {
        setDeleteBlocker(dependencies);
        return;
      }
    } catch (dependencyError) {
      void logCaughtDiagnostic("interface.notes_panel.dependencies_failed", "Note dependencies could not be checked.", dependencyError, "notes_panel");
      setError(dependencyError instanceof Error ? dependencyError.message : "Could not check whether this note is used by a report.");
      return;
    }
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
    queueAutosave();
  };

  const captureMatches = captureDraft ? notes.filter((note) => note.title.toLowerCase().includes(captureQuery.trim().toLowerCase())) : [];

  return (
    <div className="notes-panel">
      <aside className="notes-list" aria-label="Project notes">
        <header>
          <strong>Notes</strong>
          <div>
            <button className="button quiet square" type="button" aria-label="Refresh notes" disabled={loading} onClick={() => void load()}><RefreshCw className={loading ? "spin" : undefined} size={14} /></button>
            <button className="button quiet square" type="button" aria-label="Create note" disabled={saving} onClick={() => void startNote()}><Plus size={15} /></button>
          </div>
        </header>
        {notes.map((note) => <button type="button" className={note.id === selectedId ? "active" : undefined} key={note.id} disabled={saving} onClick={() => void selectNote(note.id)}><strong>{note.title}</strong><small>{note.observationType === "ai_tool_note" ? "AI-generated · " : ""}{new Date(note.updatedAt).toLocaleString()}</small></button>)}
        {loading && notes.length > 0 && <p className="notes-load-status" role="status">Refreshing notes…</p>}
        {!notes.length && !loading && <p>No notes yet.</p>}
      </aside>
      <section className={`note-editor${creating || selected ? "" : " is-empty"}`} aria-label={creating ? "New note" : selected ? `Edit ${selected.title}` : "Note editor"}>
        {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
        {deleteBlocker && <div className="callout note-dependency-blocker" role="alert"><AlertTriangle size={18} /><div><strong>This note is retained by {deleteBlocker.reports.length === 1 ? "a report" : "reports"}</strong><p>{deleteBlocker.reports.some((report) => report.status === "final") ? `Final report ${deleteBlocker.reports.filter((report) => report.status === "final").map((report) => `“${report.title}”`).join(", ")} is immutable, so Nebula retains this source note to protect its lineage.` : `Remove this note from ${deleteBlocker.reports.map((report) => `“${report.title}”`).join(", ")} before deleting it.`}</p></div><a className="button secondary" href="/reports">Open Reports</a></div>}
        {(creating || selected) ? <>
          <header>
            <input aria-label="Note title" value={draft.title} placeholder="Note title" maxLength={500} onChange={(event) => { setDraft((current) => ({ ...current, title: event.target.value })); queueAutosave(); }} />
            <div>
              {selected && <IconAction icon={Trash2} label="Delete" title="Delete note" onClick={() => void remove()} />}
              <button className="button quiet" type="button" disabled={!hasBody || !writingRuntimes.length} onClick={() => setWritingOpen(true)}><Sparkles size={14} /> Transform with AI</button>
              <button className="button quiet" type="button" disabled={!hasBody || !onAskNebula} onClick={() => onAskNebula?.({ text: draftBodyRef.current, sourceKind: "note", sourceId: selected?.id, sourceLabel: draft.title || "Untitled note" })}><MessageSquareQuote size={14} /> Ask Nebula</button>
              <button className="button primary" type="button" disabled={saving || !draft.title.trim() || saveStatus === "saved"} onClick={() => void save()}><Save size={14} /> {saveStatus === "failed" ? "Retry save" : "Save"}</button>
            </div>
          </header>
          <div className="note-editor-body">
            <div className="note-editor-caption"><span>Markdown</span><span className="note-save-status" role="status" aria-live="polite">{saving ? "Saving…" : saveStatus === "failed" ? "Save failed" : saveStatus === "unsaved" ? "Unsaved changes" : creating ? "Not saved" : "Saved"}</span></div>
            <NoteBodyEditor key={creating ? "new" : selected?.id} body={draft.body} onChange={onBodyChange} />
          </div>
          {(linksLoading || evidenceOptions.length > 0 || assetOptions.length > 0 || draft.evidenceIds.length > 0 || draft.assetIds.length > 0) && <details onToggle={(event) => setLinksOpen(event.currentTarget.open)}><summary>Links · {draft.evidenceIds.length} evidence · {draft.assetIds.length} assets</summary>
            {linksOpen && <div className="note-link-picker">
              <input type="search" aria-label="Search note links" placeholder="Search evidence and assets" value={linkQuery} onChange={(event) => setLinkQuery(event.target.value)} />
              {linksLoading && <p role="status">Loading links…</p>}
              {matchingEvidence.items.length > 0 && <div className="note-link-results" role="group" aria-label="Evidence links">{matchingEvidence.items.map((option) => <label key={option.id}><input type="checkbox" checked={draft.evidenceIds.includes(option.id)} onChange={() => toggleLink("evidenceIds", option.id)} /> {option.label}</label>)}</div>}
              {matchingEvidence.total > matchingEvidence.items.length && <p>Showing {matchingEvidence.items.length} of {matchingEvidence.total} evidence matches. Search to narrow.</p>}
              {matchingAssets.items.length > 0 && <div className="note-link-results" role="group" aria-label="Asset links">{matchingAssets.items.map((option) => <label key={option.id}><input type="checkbox" checked={draft.assetIds.includes(option.id)} onChange={() => toggleLink("assetIds", option.id)} /> {option.label}</label>)}</div>}
              {matchingAssets.total > matchingAssets.items.length && <p>Showing {matchingAssets.items.length} of {matchingAssets.total} asset matches. Search to narrow.</p>}
              {!linksLoading && matchingEvidence.total + matchingAssets.total === 0 && <p>No matching links.</p>}
            </div>}
          </details>}
        </> : <div className="note-initial-state">
          {loading ? <LoadingSurface label="Loading saved notes" /> : <StandardEmptyState className="note-empty-state" icon={<NotebookPen size={24} aria-hidden="true" />} title="Start a project note" explanation="Capture working thoughts in Markdown. Preserve exact files and screenshots as Evidence." />}
          <button className="button primary" type="button" onClick={() => void startNote()}><Plus size={14} /> New note</button>
        </div>}
      </section>
      {captureDraft && <ModalSurface className="provider-dialog note-capture-dialog" labelledBy="note-capture-title" onClose={closeCapture}>
        <header><div><small>Selected text</small><h2 id="note-capture-title">Add to a note</h2></div></header>
        <p className="provider-dialog-note">From {captureDraft.source.label}. Choose where to save it.</p>
        <pre className="note-capture-preview">{captureDraft.text}</pre>
        <label>Find a note<input type="search" data-autofocus value={captureQuery} onChange={(event) => setCaptureQuery(event.target.value)} placeholder="Search note titles" /></label>
        <div className="note-capture-targets" role="radiogroup" aria-label="Destination note">
          {captureMatches.slice(0, CAPTURE_RESULT_LIMIT).map((note) => <label key={note.id}><input type="radio" name="capture-target" value={note.id} checked={captureTargetId === note.id} onChange={() => setCaptureTargetId(note.id)} /><span>{note.title}</span></label>)}
          {loading && <p role="status">Loading saved notes…</p>}
          {!loading && captureMatches.length === 0 && <p>{notes.length ? "No notes match that title." : "No saved notes yet."}</p>}
          {captureMatches.length > CAPTURE_RESULT_LIMIT && <p>Showing {CAPTURE_RESULT_LIMIT} of {captureMatches.length} notes. Search to narrow.</p>}
          <label><input type="radio" name="capture-target" value="new" checked={captureTargetId === "new"} onChange={() => setCaptureTargetId("new")} /><span>New note</span></label>
        </div>
        {captureTargetId === "new" && <label>Note title<input required maxLength={500} value={captureTitle} onChange={(event) => setCaptureTitle(event.target.value)} /></label>}
        {captureError && <DiagnosticErrorNotice error={captureError} fallback="The selected text could not be saved." compact />}
        {error && !loading && <button className="button quiet" type="button" onClick={() => void load()}>Retry loading notes</button>}
        <footer><button className="button secondary" type="button" disabled={captureSaving} onClick={closeCapture}>Cancel</button><button className="button primary" type="button" disabled={captureSaving || saving || !captureTargetId || (captureTargetId === "new" && !captureTitle.trim())} onClick={() => void saveCapture()}>{captureSaving ? "Saving…" : captureTargetId === "new" ? "Create note" : "Add to note"}</button></footer>
      </ModalSurface>}
      {writingOpen && <AIWritingDialog
        api={api}
        engagementId={engagementId}
        providers={providers}
        harnesses={harnesses}
        purpose="note"
        title="Transform note with AI"
        description="Tell Nebula how to organize or rewrite this note. The generated text remains editable and is not persisted until you save the note."
        sourceLabel={draft.title || "Untitled note"}
        sourceText={draftBodyRef.current}
        initialInstruction="Turn this into a concise analyst note. Preserve exact observations, separate hypotheses, and keep useful technical details."
        onClose={() => setWritingOpen(false)}
        onApply={(result) => {
          draftBodyRef.current = result.content;
          queueAutosave();
          setHasBody(Boolean(result.content.trim()));
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
