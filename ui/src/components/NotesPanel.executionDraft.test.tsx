import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import type {ObservationSummary} from "../api/types";
import {DialogProvider} from "./DialogSystem";
import {NotesPanel} from "./NotesPanel";

const note: ObservationSummary = {id: "accepted-note", engagementId: "selected-project", observationType: "ai_execution_note",
  title: "Reviewed synthetic explanation", body: "Reviewed synthetic explanation", assetIds: [], evidenceIds: [], serviceIds: [],
  confidence: 1, source: "operator-accepted-ai-draft", metadata: {generated_draft_id: "draft", accepted_by: "operator",
    provenance: "ai-generated/operator-edited/operator-accepted"}, createdAt: "2026-11-02T00:00:00Z", updatedAt: "2026-11-02T00:00:00Z", revision: 3};

describe.each([false, true])("accepted execution notes (cached=%s)", cached => {
  it("selects the saved note body, retains provenance and excludes other projects", async () => {
    const listObservations = vi.fn().mockResolvedValue({items: [note], total: 1});
    render(<DialogProvider><NotesPanel api={{listObservations} as unknown as ApiClient} engagementId={note.engagementId}
      initialNotes={cached ? [note, {...note, id: "foreign", engagementId: "other", title: "Foreign note"}] : []}
      initialNotesReady={cached} /></DialogProvider>);
    expect(await screen.findByRole("button", {name: /Reviewed synthetic explanation/})).toBeVisible();
    // The list, selected record and controlled editor synchronize in effects.
    await waitFor(() => expect(screen.getByRole("textbox", {name: "Note body"})).toHaveValue(note.body));
    expect(screen.getByText(/AI-generated · Operator accepted/)).toBeVisible();
    expect(screen.queryByRole("button", {name: /Foreign note/})).not.toBeInTheDocument();
    expect(listObservations).toHaveBeenCalledTimes(cached ? 0 : 1);
  });
});

it("appends selected text using the accepted note's current Core revision and preserves provenance", async () => {
  const user = userEvent.setup();
  const current = {...note, body: "Current reviewed body", revision: 4};
  const listObservations = vi.fn().mockResolvedValue({items: [current], total: 1});
  const updateObservation = vi.fn().mockImplementation(async (_id, changes) => ({...current, ...changes, revision: 5}));
  render(<DialogProvider><NotesPanel api={{listObservations, updateObservation} as unknown as ApiClient}
    engagementId={note.engagementId} initialNotes={[note]} initialNotesReady
    initialDraft={{text: "Additional operator context", originalLength: 27, truncated: false,
      source: {kind: "terminal", id: "synthetic", label: "Synthetic selection"}, anchor: {left: 0, top: 0, right: 0, bottom: 0}}} /></DialogProvider>);
  await user.click(await screen.findByRole("radio", {name: /Reviewed synthetic explanation/}));
  await user.click(screen.getByRole("button", {name: "Add to note"}));
  await waitFor(() => expect(updateObservation).toHaveBeenCalledWith(note.id, expect.objectContaining({expectedRevision: 4,
    body: "Current reviewed body\n\nAdditional operator context", metadata: expect.objectContaining(note.metadata)})));
  await waitFor(() => expect(screen.getByRole("textbox", {name: "Note body"})).toHaveValue("Current reviewed body\n\nAdditional operator context"));
});
