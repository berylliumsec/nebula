import {ApiError, type ApiClient} from "../api/client";
import type {ObservationSummary} from "../api/types";
import {isProjectNoteType} from "../api/noteTypes";
import {logCaughtDiagnostic} from "../diagnostics";
import type {SettingClientAction} from "./nebulaAssistantActions";

type NoteAction = Extract<SettingClientAction, {kind: "note_create" | "note_update" | "note_delete"}>;
type NoteApi = Pick<ApiClient, "getObservation" | "createButlerNote" | "updateObservation" | "deleteObservation" | "observationDependencies">;
type Result = {message: string; refresh: boolean};

function matches(note: ObservationSummary, changes: {title?: string; body?: string; assetIds?: string[]; evidenceIds?: string[]}): boolean {
  return (changes.title === undefined || note.title === changes.title)
    && (changes.body === undefined || note.body === changes.body)
    && (changes.assetIds === undefined || JSON.stringify([...note.assetIds].sort()) === JSON.stringify([...changes.assetIds].sort()))
    && (changes.evidenceIds === undefined || JSON.stringify([...note.evidenceIds].sort()) === JSON.stringify([...changes.evidenceIds].sort()));
}

/** Reconcile exact note IDs through Core; only deletion needs browser confirmation. */
export async function applyButlerNoteAction(
  api: NoteApi, action: NoteAction, selectedProject: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string; tone?: "danger"}) => Promise<boolean>,
): Promise<Result> {
  const selected = () => selectedProject() === action.projectId;
  if (!selected()) throw new Error("Select the original project before changing its note.");
  const read = async (): Promise<ObservationSummary | undefined> => {
    let note: ObservationSummary;
    try {
      note = await api.getObservation(action.noteId);
    } catch (caught) {
      // diagnostic-expected: absence is authoritative create preflight or delete readback.
      if (caught instanceof ApiError && caught.status === 404) return undefined;
      throw caught;
    }
    if (note.id !== action.noteId || note.engagementId !== action.projectId
        || !isProjectNoteType(note.observationType)) {
      throw new Error("The note identity does not match the selected project.");
    }
    return note;
  };
  const current = async (): Promise<ObservationSummary> => {
    const note = await read();
    if (!selected()) throw new Error("The selected project changed. Read the note again.");
    if (!note || action.kind === "note_create" || note.revision !== action.expectedRevision || note.title !== action.name) {
      throw new Error("The note changed. Ask Butler to read its current title and revision.");
    }
    return note;
  };
  if (action.kind === "note_create") {
    const existing = await read();
    if (!selected()) throw new Error("The selected project changed. Read the note again.");
    if (existing) {
      if (!matches(existing, action)) throw new Error("This note ID already has different content. Inspect it before retrying.");
      return {message: `Core already saved note “${existing.title}” at revision ${existing.revision}.`, refresh: true};
    }
  } else await current();
  if (!selected()) throw new Error("The selected project changed. Read the note again.");

  if (action.kind === "note_delete") {
    const checkDependencies = async () => {
      const dependencies = await api.observationDependencies(action.noteId);
      if (dependencies.observationId !== action.noteId || !dependencies.deletable) {
        throw new Error("Reports still use this note. Remove those links before deleting it.");
      }
    };
    await checkDependencies();
    if (!await confirm({title: "Delete project note?", message: `Permanently delete “${action.name}”? Its linked evidence and assets are retained.`, confirmLabel: "Delete note", tone: "danger"})) {
      return {message: `Note “${action.name}” was not deleted.`, refresh: false};
    }
    await current();
    await checkDependencies();
  }
  if (!selected()) throw new Error("The selected project changed. Read the note again.");
  try {
    if (action.kind === "note_create") await api.createButlerNote(action.noteId, {
      engagementId: action.projectId, title: action.title, body: action.body, assetIds: action.assetIds, evidenceIds: action.evidenceIds,
    });
    else if (action.kind === "note_update") await api.updateObservation(action.noteId, {
      expectedRevision: action.expectedRevision,
      ...(action.title !== undefined ? {title: action.title} : {}), ...(action.body !== undefined ? {body: action.body} : {}),
      ...(action.assetIds !== undefined ? {assetIds: action.assetIds} : {}), ...(action.evidenceIds !== undefined ? {evidenceIds: action.evidenceIds} : {}),
    });
    else await api.deleteObservation(action.noteId, action.expectedRevision);
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_notes.write_response_failed", "The note mutation response failed; reading exact Core state before retry.", caught, "notes");
  }
  let observed: ObservationSummary | undefined;
  try {
    observed = await read();
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_notes.readback_failed", "The note mutation could not be read back.", caught, "notes");
    return {message: "Core did not confirm the note change. Inspect Project notes before retrying; the write may have succeeded.", refresh: true};
  }
  if (!selected()) return {message: "The selected project changed during the note action. Inspect the original project's Notes before retrying.", refresh: false};
  if (action.kind === "note_delete") {
    return {message: observed ? "Core did not confirm note deletion. Read its current state before retrying."
      : `Deleted note “${action.name}”. Core confirms its absence; linked assets and evidence remain.`, refresh: true};
  }
  if (!observed || !matches(observed, action) || (action.kind === "note_update" && observed.revision <= action.expectedRevision)) {
    return {message: "Core did not confirm the requested note fields. Read its current state before retrying.", refresh: true};
  }
  return {message: `${action.kind === "note_create" ? "Created" : "Updated"} note “${observed.title}” at Core revision ${observed.revision}. Core confirmed its saved fields and links.`, refresh: true};
}
