import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { ObservationSummary } from "../api/types";
import { DialogProvider } from "./DialogSystem";
import { NotesPanel } from "./NotesPanel";

const note: ObservationSummary = {
  id: "note-1",
  engagementId: "eng-1",
  observationType: "note",
  title: "Initial note",
  body: "**Useful** context",
  assetIds: [],
  serviceIds: [],
  evidenceIds: [],
  confidence: 1,
  metadata: {},
  createdAt: "2026-07-13T12:00:00Z",
  updatedAt: "2026-07-13T12:00:00Z",
  revision: 1,
};

function renderPanel(api: Partial<ApiClient>, onAskNebula = vi.fn()) {
  return render(<DialogProvider><NotesPanel api={api as ApiClient} engagementId="eng-1" onAskNebula={onAskNebula} /></DialogProvider>);
}

describe("NotesPanel", () => {
  it("loads, revision-safely edits, and drafts note context without sending", async () => {
    const user = userEvent.setup();
    const updateObservation = vi.fn().mockResolvedValue({ ...note, body: "Changed", revision: 2 });
    const onAsk = vi.fn();
    renderPanel({
      listObservations: vi.fn().mockResolvedValue({ items: [note], total: 1 }),
      updateObservation,
    }, onAsk);

    const body = await screen.findByRole("textbox", { name: "Note body" });
    await waitFor(() => expect(body).toHaveValue("**Useful** context"));
    expect(screen.queryByRole("region", { name: "Note preview" })).not.toBeInTheDocument();
    await user.clear(body);
    await user.type(body, "Changed");
    await user.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(updateObservation).toHaveBeenCalledWith("note-1", expect.objectContaining({ body: "Changed", expectedRevision: 1 })));

    await user.click(screen.getByRole("button", { name: /ask nebula/i }));
    expect(onAsk).toHaveBeenCalledWith(expect.objectContaining({ text: "Changed", sourceKind: "note", sourceId: "note-1" }));
  });

  it("creates a plaintext Markdown note", async () => {
    const user = userEvent.setup();
    const createObservation = vi.fn().mockResolvedValue({ ...note, id: "note-2", title: "New", body: "# Body" });
    renderPanel({
      listObservations: vi.fn().mockResolvedValue({ items: [], total: 0 }),
      createObservation,
    });

    await user.click(await screen.findByRole("button", { name: "New note" }));
    await user.type(screen.getByRole("textbox", { name: "Note title" }), "New");
    await user.type(screen.getByRole("textbox", { name: "Note body" }), "# Body");
    await user.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(createObservation).toHaveBeenCalledWith(expect.objectContaining({ engagementId: "eng-1", observationType: "note", title: "New", body: "# Body" })));
  });

  it("saves selected text immediately as an editable project note", async () => {
    const createObservation = vi.fn().mockResolvedValue({
      ...note,
      id: "selection-note",
      title: "Note from Terminal selection",
      body: "whoami\n",
      source: "selection-note",
    });
    const consumed = vi.fn();
    render(<StrictMode><DialogProvider><NotesPanel
      api={{
        listObservations: vi.fn().mockResolvedValue({ items: [], total: 0 }),
        createObservation,
      } as unknown as ApiClient}
      engagementId="eng-1"
      initialDraft={{
        text: "whoami\n",
        originalLength: 7,
        truncated: false,
        source: { kind: "terminal", id: "terminal-1", label: "Terminal selection" },
        anchor: { left: 0, top: 0, right: 0, bottom: 0 },
      }}
      onInitialDraftConsumed={consumed}
    /></DialogProvider></StrictMode>);

    expect(await screen.findByRole("textbox", { name: "Note title" })).toHaveValue("Note from Terminal selection");
    expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue("whoami\n");
    expect(consumed).toHaveBeenCalledOnce();
    await waitFor(() => expect(createObservation).toHaveBeenCalledWith(expect.objectContaining({
      engagementId: "eng-1",
      observationType: "note",
      title: "Note from Terminal selection",
      body: "whoami\n",
      source: "selection-note",
      metadata: expect.objectContaining({ selection_source: expect.objectContaining({ kind: "terminal", id: "terminal-1" }) }),
    })));
    expect(createObservation).toHaveBeenCalledOnce();
  });
});
