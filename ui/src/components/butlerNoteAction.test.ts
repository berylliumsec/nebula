import {describe, expect, it, vi} from "vitest";
import {ApiError, type ApiClient} from "../api/client";
import type {ObservationSummary} from "../api/types";
import {applyButlerNoteAction} from "./butlerNoteAction";

const noteId = "b96e898d-207f-4943-846e-c00f4415fc26";
const note: ObservationSummary = {id: noteId, engagementId: "project", observationType: "note", title: "Fixture note", body: "Saved body",
  assetIds: [], evidenceIds: [], serviceIds: [], confidence: 1, metadata: {keep: true}, createdAt: "", updatedAt: "", revision: 3};
const create = {kind: "note_create" as const, projectId: "project", noteId, title: note.title, body: note.body, assetIds: [], evidenceIds: []};
const update = {kind: "note_update" as const, projectId: "project", noteId, name: note.title, expectedRevision: 3, body: "Edited body"};
const remove = {kind: "note_delete" as const, projectId: "project", noteId, name: note.title, expectedRevision: 3};
const missing = () => new ApiError("Missing note", 404);
const dependencies = {observationId: noteId, deletable: true, reports: []};

describe("Butler note handoff", () => {
  it("creates a browser-owned ID and verifies exact Core fields", async () => {
    const api = {getObservation: vi.fn().mockRejectedValueOnce(missing()).mockResolvedValue(note),
      createButlerNote: vi.fn().mockResolvedValue(note)} as unknown as ApiClient;
    expect((await applyButlerNoteAction(api, create, () => "project", async () => true)).message).toContain("Created note");
    expect(api.createButlerNote).toHaveBeenCalledWith(noteId, {engagementId: "project", title: note.title, body: note.body, assetIds: [], evidenceIds: []});
  });

  it("reconciles a lost create response and an exact repeated handoff without duplicating", async () => {
    const api = {getObservation: vi.fn().mockRejectedValueOnce(missing()).mockResolvedValue(note),
      createButlerNote: vi.fn().mockRejectedValue(new Error("lost response"))} as unknown as ApiClient;
    expect((await applyButlerNoteAction(api, create, () => "project", async () => true)).message).toContain("Core confirmed");
    expect((await applyButlerNoteAction(api, create, () => "project", async () => true)).message).toContain("already saved");
    expect(api.createButlerNote).toHaveBeenCalledTimes(1);
  });

  it("refuses a stale note or switched project before writing", async () => {
    const api = {getObservation: vi.fn().mockResolvedValue({...note, revision: 4}), updateObservation: vi.fn()} as unknown as ApiClient;
    await expect(applyButlerNoteAction(api, update, () => "project", async () => true)).rejects.toThrow("note changed");
    await expect(applyButlerNoteAction(api, update, () => "other", async () => true)).rejects.toThrow("original project");
    expect(api.updateObservation).not.toHaveBeenCalled();
  });

  it.each(["note", "ai_tool_note", "ai_execution_note"])("updates only requested fields of a %s and checks the next saved revision", async observationType => {
    const current = {...note, observationType};
    const saved = {...current, body: update.body, revision: 4};
    const api = {getObservation: vi.fn().mockResolvedValueOnce(current).mockResolvedValue(saved),
      updateObservation: vi.fn().mockResolvedValue(saved)} as unknown as ApiClient;
    expect((await applyButlerNoteAction(api, update, () => "project", async () => true)).message).toContain("Updated note");
    expect(api.updateObservation).toHaveBeenCalledWith(noteId, {expectedRevision: 3, body: update.body});
  });

  it("refuses a saved non-note observation even when its identity and revision match", async () => {
    const api = {getObservation: vi.fn().mockResolvedValue({...note, observationType: "execution_result"}),
      updateObservation: vi.fn()} as unknown as ApiClient;
    await expect(applyButlerNoteAction(api, update, () => "project", async () => true)).rejects.toThrow("note identity");
    expect(api.updateObservation).not.toHaveBeenCalled();
  });

  it("cancels deletion without changing Core", async () => {
    const api = {getObservation: vi.fn().mockResolvedValue(note), observationDependencies: vi.fn().mockResolvedValue(dependencies), deleteObservation: vi.fn()} as unknown as ApiClient;
    expect((await applyButlerNoteAction(api, remove, () => "project", async () => false)).message).toContain("was not deleted");
    expect(api.deleteObservation).not.toHaveBeenCalled();
  });

  it("keeps report-linked notes behind Core dependency guards", async () => {
    const api = {getObservation: vi.fn().mockResolvedValue(note), observationDependencies: vi.fn().mockResolvedValue({...dependencies, deletable: false}), deleteObservation: vi.fn()} as unknown as ApiClient;
    await expect(applyButlerNoteAction(api, remove, () => "project", async () => true)).rejects.toThrow("Reports still use");
    expect(api.deleteObservation).not.toHaveBeenCalled();
  });

  it("claims deletion only after Core returns exact absence", async () => {
    const api = {getObservation: vi.fn().mockResolvedValueOnce(note).mockResolvedValueOnce(note).mockRejectedValue(missing()),
      observationDependencies: vi.fn().mockResolvedValue(dependencies), deleteObservation: vi.fn().mockResolvedValue(undefined)} as unknown as ApiClient;
    expect((await applyButlerNoteAction(api, remove, () => "project", async () => true)).message).toContain("Core confirms its absence");
    expect(api.deleteObservation).toHaveBeenCalledWith(noteId, 3);
  });
});
