import {ApiError, type ApiClient} from "../api/client";
import type {ObservationSummary} from "../api/types";
import {isProjectNoteType} from "../api/noteTypes";
import {logCaughtDiagnostic} from "../diagnostics";
import type {SettingClientAction} from "./nebulaAssistantActions";

// Core saves Butler note creates and edits itself (`note_saved`); only deletion
// still needs this browser's confirmation.
type NoteAction = Extract<SettingClientAction, {kind: "note_delete"}>;
type NoteApi = Pick<ApiClient, "getObservation" | "deleteObservation" | "observationDependencies">;
type Result = {message: string; refresh: boolean};

/** Delete an exact note after browser confirmation and read its absence back from Core. */
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
      // diagnostic-expected: absence is the authoritative delete readback.
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
    if (!note || note.revision !== action.expectedRevision || note.title !== action.name) {
      throw new Error("The note changed. Ask Butler to read its current title and revision.");
    }
    return note;
  };
  const checkDependencies = async () => {
    const dependencies = await api.observationDependencies(action.noteId);
    if (dependencies.observationId !== action.noteId || !dependencies.deletable) {
      throw new Error("Reports still use this note. Remove those links before deleting it.");
    }
  };
  await current();
  await checkDependencies();
  if (!await confirm({title: "Delete project note?", message: `Permanently delete “${action.name}”? Its linked evidence and assets are retained.`, confirmLabel: "Delete note", tone: "danger"})) {
    return {message: `Note “${action.name}” was not deleted.`, refresh: false};
  }
  await current();
  await checkDependencies();
  if (!selected()) throw new Error("The selected project changed. Read the note again.");
  try {
    await api.deleteObservation(action.noteId, action.expectedRevision);
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_notes.write_response_failed", "The note deletion response failed; reading exact Core state before retry.", caught, "notes");
  }
  let observed: ObservationSummary | undefined;
  try {
    observed = await read();
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_notes.readback_failed", "The note deletion could not be read back.", caught, "notes");
    return {message: "Core did not confirm the note change. Inspect Project notes before retrying; the write may have succeeded.", refresh: true};
  }
  if (!selected()) return {message: "The selected project changed during the note action. Inspect the original project's Notes before retrying.", refresh: false};
  return {message: observed ? "Core did not confirm note deletion. Read its current state before retrying."
    : `Deleted note “${action.name}”. Core confirms its absence; linked assets and evidence remain.`, refresh: true};
}
