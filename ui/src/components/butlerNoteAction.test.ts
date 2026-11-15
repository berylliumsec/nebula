import {describe, expect, it, vi} from "vitest";
import {ApiError, type ApiClient} from "../api/client";
import type {ObservationSummary} from "../api/types";
import {applyButlerNoteAction} from "./butlerNoteAction";

const noteId = "b96e898d-207f-4943-846e-c00f4415fc26";
const note: ObservationSummary = {id: noteId, engagementId: "project", observationType: "note", title: "Fixture note", body: "Saved body",
  assetIds: [], evidenceIds: [], serviceIds: [], confidence: 1, metadata: {keep: true}, createdAt: "", updatedAt: "", revision: 3};
const remove = {kind: "note_delete" as const, projectId: "project", noteId, name: note.title, expectedRevision: 3};
const missing = () => new ApiError("Missing note", 404);
const dependencies = {observationId: noteId, deletable: true, reports: []};

describe("Butler note deletion handoff", () => {
  it("refuses a stale note or switched project before confirming", async () => {
    const confirm = vi.fn().mockResolvedValue(true);
    const api = {getObservation: vi.fn().mockResolvedValue({...note, revision: 4}), observationDependencies: vi.fn(), deleteObservation: vi.fn()} as unknown as ApiClient;
    await expect(applyButlerNoteAction(api, remove, () => "project", confirm)).rejects.toThrow("note changed");
    await expect(applyButlerNoteAction(api, remove, () => "other", confirm)).rejects.toThrow("original project");
    expect(confirm).not.toHaveBeenCalled();
    expect(api.deleteObservation).not.toHaveBeenCalled();
  });

  it("refuses a saved non-note observation even when its identity and revision match", async () => {
    const api = {getObservation: vi.fn().mockResolvedValue({...note, observationType: "execution_result"}),
      observationDependencies: vi.fn(), deleteObservation: vi.fn()} as unknown as ApiClient;
    await expect(applyButlerNoteAction(api, remove, () => "project", async () => true)).rejects.toThrow("note identity");
    expect(api.deleteObservation).not.toHaveBeenCalled();
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

  it.each(["note", "ai_tool_note", "ai_execution_note"])("claims deletion of a %s only after Core returns exact absence", async observationType => {
    const current = {...note, observationType};
    const api = {getObservation: vi.fn().mockResolvedValueOnce(current).mockResolvedValueOnce(current).mockRejectedValue(missing()),
      observationDependencies: vi.fn().mockResolvedValue(dependencies), deleteObservation: vi.fn().mockResolvedValue(undefined)} as unknown as ApiClient;
    expect((await applyButlerNoteAction(api, remove, () => "project", async () => true)).message).toContain("Core confirms its absence");
    expect(api.deleteObservation).toHaveBeenCalledWith(noteId, 3);
  });

  it("does not claim deletion while Core still returns the note", async () => {
    const api = {getObservation: vi.fn().mockResolvedValue(note),
      observationDependencies: vi.fn().mockResolvedValue(dependencies), deleteObservation: vi.fn().mockRejectedValue(new Error("lost response"))} as unknown as ApiClient;
    expect((await applyButlerNoteAction(api, remove, () => "project", async () => true)).message).toContain("did not confirm note deletion");
  });
});
