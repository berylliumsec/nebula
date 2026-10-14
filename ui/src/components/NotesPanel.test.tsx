import { render, screen, waitFor, within } from "@testing-library/react";
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
const selectionDraft = {
  text: "whoami\n",
  originalLength: 7,
  truncated: false,
  source: { kind: "terminal", id: "terminal-1", label: "Terminal selection" },
  anchor: { left: 0, top: 0, right: 0, bottom: 0 },
};

function renderPanel(api: Partial<ApiClient>, onAskNebula = vi.fn()) {
  return render(<DialogProvider><NotesPanel api={api as ApiClient} engagementId="eng-1" onAskNebula={onAskNebula} /></DialogProvider>);
}

describe("NotesPanel", () => {
  it("autosaves a new note after typing stops", async () => {
    const user = userEvent.setup();
    const createObservation = vi.fn().mockImplementation(async (request) => ({ ...note, ...request, id: "new-note" }));
    renderPanel({ listObservations: vi.fn().mockResolvedValue({ items: [], total: 0 }), createObservation });
    await user.click(await screen.findByRole("button", { name: "New note" }));
    await user.type(screen.getByRole("textbox", { name: "Note title" }), "Draft");
    await user.type(screen.getByRole("textbox", { name: "Note body" }), "Body");
    await waitFor(() => expect(createObservation).toHaveBeenCalledWith(expect.objectContaining({ title: "Draft", body: "Body" })), { timeout: 2500 });
    await waitFor(() => expect(screen.getByText("Saved")).toBeVisible());
  });

  it("keeps text entered while a new note is being created", async () => {
    const user = userEvent.setup();
    let finishCreate!: (value: ObservationSummary) => void;
    const createObservation = vi.fn().mockImplementation(() => new Promise<ObservationSummary>((resolve) => { finishCreate = resolve; }));
    const updateObservation = vi.fn().mockImplementation(async (_id, request) => ({ ...note, ...request, id: "new-note", revision: 2 }));
    renderPanel({ listObservations: vi.fn().mockResolvedValue({ items: [], total: 0 }), createObservation, updateObservation });
    await user.click(await screen.findByRole("button", { name: "New note" }));
    await user.type(screen.getByRole("textbox", { name: "Note title" }), "Draft");
    const body = screen.getByRole("textbox", { name: "Note body" });
    await user.type(body, "Before");
    await waitFor(() => expect(createObservation).toHaveBeenCalledWith(expect.objectContaining({ body: "Before" })), { timeout: 2500 });
    await user.type(body, " after");
    finishCreate({ ...note, id: "new-note", title: "Draft", body: "Before" });
    await waitFor(() => expect(updateObservation).toHaveBeenCalledWith("new-note", expect.objectContaining({ body: "Before after", expectedRevision: 1 })), { timeout: 2500 });
    expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue("Before after");
  });

  it("autosaves an existing note and keeps text typed during the save", async () => {
    const user = userEvent.setup();
    let finishFirst!: (value: ObservationSummary) => void;
    const updateObservation = vi.fn()
      .mockImplementationOnce(() => new Promise<ObservationSummary>((resolve) => { finishFirst = resolve; }))
      .mockImplementation(async (_id, request) => ({ ...note, ...request, revision: 3 }));
    renderPanel({ listObservations: vi.fn().mockResolvedValue({ items: [note], total: 1 }), updateObservation });
    const body = await screen.findByRole("textbox", { name: "Note body" });
    await waitFor(() => expect(body).toHaveValue(note.body));
    await user.clear(body);
    await user.type(body, "First change");
    await waitFor(() => expect(updateObservation).toHaveBeenCalledWith(note.id, expect.objectContaining({ body: "First change", expectedRevision: 1 })), { timeout: 2500 });
    await user.type(body, " later");
    finishFirst({ ...note, body: "First change", revision: 2 });
    await waitFor(() => expect(updateObservation).toHaveBeenNthCalledWith(2, note.id, expect.objectContaining({ body: "First change later", expectedRevision: 2 })), { timeout: 2500 });
    expect(body).toHaveValue("First change later");
  });

  it("keeps the current note selected when more text arrives during a switch save", async () => {
    const user = userEvent.setup();
    const second = { ...note, id: "note-2", title: "Second note", body: "Second body" };
    let finishFirst!: (value: ObservationSummary) => void;
    const updateObservation = vi.fn()
      .mockImplementationOnce(() => new Promise<ObservationSummary>((resolve) => { finishFirst = resolve; }))
      .mockImplementation(async (_id, request) => ({ ...note, ...request, revision: 3 }));
    renderPanel({ listObservations: vi.fn().mockResolvedValue({ items: [note, second], total: 2 }), updateObservation });
    const body = await screen.findByRole("textbox", { name: "Note body" });
    await waitFor(() => expect(body).toHaveValue(note.body));
    await user.clear(body);
    await user.type(body, "First change");
    await user.click(screen.getByRole("button", { name: /Second note/ }));
    await user.type(body, " later");
    finishFirst({ ...note, body: "First change", revision: 2 });
    await waitFor(() => expect(updateObservation).toHaveBeenNthCalledWith(2, note.id, expect.objectContaining({ body: "First change later", expectedRevision: 2 })), { timeout: 2500 });
    expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue("First change later");
    expect(screen.getByRole("button", { name: /Initial note/ })).toHaveClass("active");
  });

  it("offers retry after autosave fails", async () => {
    const user = userEvent.setup();
    const updateObservation = vi.fn().mockRejectedValueOnce(new Error("Save unavailable")).mockResolvedValue({ ...note, body: "Retry text", revision: 2 });
    renderPanel({ listObservations: vi.fn().mockResolvedValue({ items: [note], total: 1 }), updateObservation });
    const body = await screen.findByRole("textbox", { name: "Note body" });
    await waitFor(() => expect(body).toHaveValue(note.body));
    await user.clear(body);
    await user.type(body, "Retry text");
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry save" })).toBeVisible(), { timeout: 2500 });
    await user.click(screen.getByRole("button", { name: "Retry save" }));
    await waitFor(() => expect(updateObservation).toHaveBeenCalledTimes(2));
    expect(body).toHaveValue("Retry text");
  });

  it("uses the loaded workspace notes without fetching them again", async () => {
    const listObservations = vi.fn();
    render(<DialogProvider><NotesPanel
      api={{ listObservations } as unknown as ApiClient}
      engagementId="eng-1"
      initialNotes={[note]}
      initialNotesReady
    /></DialogProvider>);

    expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue(note.body);
    expect(screen.getByRole("button", { name: /Initial note/ })).toHaveClass("active");
    expect(listObservations).not.toHaveBeenCalled();
    expect(screen.getByText("Saved").closest(".note-editor-caption")).toBeVisible();
  });

  it("waits for the workspace's in-flight notes request without starting another", async () => {
    const listObservations = vi.fn();
    const api = { listObservations } as unknown as ApiClient;
    const { rerender } = render(<DialogProvider><NotesPanel api={api} engagementId="eng-1" initialNotesLoading /></DialogProvider>);
    expect(screen.getByRole("status", { name: "Loading saved notes" })).toBeVisible();
    expect(listObservations).not.toHaveBeenCalled();

    rerender(<DialogProvider><NotesPanel api={api} engagementId="eng-1" initialNotes={[note]} initialNotesReady /></DialogProvider>);
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue(note.body));
    expect(listObservations).not.toHaveBeenCalled();
  });

  it("keeps large link catalogs collapsed and lets an operator find and select a later item", async () => {
    const user = userEvent.setup();
    const updateObservation = vi.fn().mockImplementation(async (_id, request) => ({ ...note, ...request, revision: 2 }));
    const evidenceOptions = Array.from({ length: 1000 }, (_, index) => ({ id: `evidence-${index}`, label: `Screenshot ${index}` }));
    render(<DialogProvider><NotesPanel api={{ listObservations: vi.fn().mockResolvedValue({ items: [note], total: 1 }) } as unknown as ApiClient}
      engagementId="eng-1" evidenceOptions={evidenceOptions} updateObservation={updateObservation} /></DialogProvider>);
    await screen.findByRole("textbox", { name: "Note body" });
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();

    await user.click(screen.getByText(/Links ·/));
    await waitFor(() => expect(screen.getAllByRole("checkbox")).toHaveLength(40));
    expect(screen.getByText("Showing 40 of 1000 evidence matches. Search to narrow.")).toBeVisible();
    await user.type(screen.getByRole("searchbox", { name: "Search note links" }), "Screenshot 999");
    await user.click(screen.getByRole("checkbox", { name: "Screenshot 999" }));
    await waitFor(() => expect(updateObservation).toHaveBeenCalledWith(note.id, expect.objectContaining({ evidenceIds: ["evidence-999"] })), { timeout: 2500 });
  });

  it("shows loading progress and lets the operator start writing before saved notes arrive", async () => {
    const user = userEvent.setup();
    let resolveNotes!: (value: { items: ObservationSummary[]; total: number }) => void;
    const listObservations = vi.fn().mockReturnValue(new Promise((resolve) => { resolveNotes = resolve; }));
    renderPanel({ listObservations });

    expect(await screen.findByRole("status")).toHaveTextContent("Loading saved notes");
    expect(screen.getByRole("status", { name: "Loading saved notes" })).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Note body" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New note" }));
    await user.type(screen.getByRole("textbox", { name: "Note body" }), "Draft during load");
    resolveNotes({ items: [note], total: 1 });
    await waitFor(() => expect(screen.queryByRole("status", { name: "Loading saved notes" })).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue("Draft during load");
  });

  it("retries a failed note load from the pane", async () => {
    const user = userEvent.setup();
    const listObservations = vi.fn()
      .mockRejectedValueOnce(new Error("Could not reach saved notes"))
      .mockResolvedValue({ items: [note], total: 1 });
    renderPanel({ listObservations });

    expect(await screen.findByText("Could not reach saved notes")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Refresh notes" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue(note.body));
    expect(screen.queryByText("Could not reach saved notes")).not.toBeInTheDocument();
  });

  it("names a finalized report that retains a note and directs the user to Reports", async () => {
    const user = userEvent.setup();
    const deleteObservation = vi.fn();
    renderPanel({
      listObservations: vi.fn().mockResolvedValue({ items: [note], total: 1 }),
      observationDependencies: vi.fn().mockResolvedValue({
        observationId: note.id,
        deletable: false,
        reports: [{ id: "report-1", title: "Client Final", status: "final" }],
      }),
      deleteObservation,
    });

    await user.click(await screen.findByRole("button", { name: "Delete" }));

    expect(await screen.findByText("This note is retained by a report")).toBeVisible();
    expect(screen.getByText(/Final report “Client Final” is immutable/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Reports" })).toHaveAttribute("href", "/reports");
    expect(deleteObservation).not.toHaveBeenCalled();
  });

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

  it("keeps the latest typed body through link edits and uses it for Save and Ask Nebula", async () => {
    const user = userEvent.setup();
    const updateObservation = vi.fn().mockImplementation(async (_id, request) => ({ ...note, ...request, revision: 2 }));
    const onAsk = vi.fn();
    render(<DialogProvider><NotesPanel
      api={{ listObservations: vi.fn().mockResolvedValue({ items: [note], total: 1 }) } as unknown as ApiClient}
      engagementId="eng-1"
      evidenceOptions={[{ id: "evidence-1", label: "Screenshot" }]}
      updateObservation={updateObservation}
      onAskNebula={onAsk}
    /></DialogProvider>);

    const body = await screen.findByRole("textbox", { name: "Note body" });
    await waitFor(() => expect(body).toHaveValue(note.body));
    await user.clear(body);
    await user.type(body, "Latest unsaved text");
    await user.click(screen.getByText(/Links ·/));
    await user.click(screen.getByRole("checkbox", { name: "Screenshot" }));
    await user.click(screen.getByRole("button", { name: "Ask Nebula" }));
    expect(onAsk).toHaveBeenCalledWith(expect.objectContaining({ text: "Latest unsaved text" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(updateObservation).toHaveBeenCalledWith("note-1", expect.objectContaining({
      body: "Latest unsaved text", evidenceIds: ["evidence-1"], expectedRevision: 1,
    })));
  });

  it("keeps unsaved edits when the note list is refreshed", async () => {
    const user = userEvent.setup();
    const listObservations = vi.fn().mockImplementation(async () => ({ items: [{ ...note }], total: 1 }));
    renderPanel({ listObservations });

    const body = await screen.findByRole("textbox", { name: "Note body" });
    await waitFor(() => expect(body).toHaveValue("**Useful** context"));
    await user.clear(body);
    await user.type(body, "Draft in progress");
    await user.click(screen.getByRole("button", { name: "Refresh notes" }));
    await waitFor(() => expect(listObservations).toHaveBeenCalledTimes(2));

    expect(body).toHaveValue("Draft in progress");
    expect(screen.getByRole("button", { name: /Initial note/ })).toHaveClass("active");
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

  it("creates a selected-text note only after the operator chooses New note", async () => {
    const user = userEvent.setup();
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
      initialDraft={selectionDraft}
      onInitialDraftConsumed={consumed}
    /></DialogProvider></StrictMode>);

    const dialog = await screen.findByRole("dialog", { name: "Add to a note" });
    expect(dialog).toHaveTextContent("whoami");
    expect(createObservation).not.toHaveBeenCalled();
    await user.click(screen.getByRole("radio", { name: "New note" }));
    expect(screen.getByRole("textbox", { name: "Note title" })).toHaveValue("Note from Terminal selection");
    await user.click(within(dialog).getByRole("button", { name: "Create note" }));
    await waitFor(() => expect(createObservation).toHaveBeenCalledWith(expect.objectContaining({
      engagementId: "eng-1",
      observationType: "note",
      title: "Note from Terminal selection",
      body: "whoami\n",
      source: "selection-note",
      metadata: expect.objectContaining({ selection_source: expect.objectContaining({ kind: "terminal", id: "terminal-1" }) }),
    })));
    expect(createObservation).toHaveBeenCalledOnce();
    expect(consumed).toHaveBeenCalledOnce();
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue("whoami\n");
  });

  it("adds selected text to the chosen existing note using its latest revision", async () => {
    const user = userEvent.setup();
    const other = { ...note, id: "note-2", title: "Chosen note", body: "Fresh original", revision: 2 };
    const listObservations = vi.fn()
      .mockResolvedValueOnce({ items: [note, { ...other, body: "Older original", revision: 1 }], total: 2 })
      .mockResolvedValue({ items: [note, other], total: 2 });
    const createObservation = vi.fn();
    const updateObservation = vi.fn().mockImplementation(async (id, request) => ({ ...other, id, body: request.body, revision: 3 }));
    render(<DialogProvider><NotesPanel api={{ listObservations, createObservation, updateObservation } as unknown as ApiClient}
      engagementId="eng-1" initialDraft={selectionDraft} /></DialogProvider>);

    const dialog = await screen.findByRole("dialog", { name: "Add to a note" });
    await user.type(screen.getByRole("searchbox", { name: "Find a note" }), "Chosen");
    await user.click(await screen.findByRole("radio", { name: "Chosen note" }));
    await user.click(screen.getByRole("button", { name: "Add to note" }));
    await waitFor(() => expect(updateObservation).toHaveBeenCalledWith("note-2", expect.objectContaining({ body: "Fresh original\n\nwhoami\n", expectedRevision: 2 })));
    expect(createObservation).not.toHaveBeenCalled();
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue("Fresh original\n\nwhoami\n");
    expect(screen.getByRole("button", { name: /Chosen note/ })).toHaveClass("active");
  });

  it("keeps a failed selected-text append available for retry", async () => {
    const user = userEvent.setup();
    const listObservations = vi.fn().mockResolvedValue({ items: [note], total: 1 });
    const updateObservation = vi.fn()
      .mockRejectedValueOnce(new Error("Save unavailable"))
      .mockImplementation(async (_id, request) => ({ ...note, body: request.body, revision: 2 }));
    render(<DialogProvider><NotesPanel api={{ listObservations, updateObservation } as unknown as ApiClient}
      engagementId="eng-1" initialDraft={selectionDraft} /></DialogProvider>);
    await user.click(await screen.findByRole("radio", { name: "Initial note" }));
    await user.click(screen.getByRole("button", { name: "Add to note" }));
    expect(await screen.findByText("Save unavailable")).toBeVisible();
    expect(screen.getByRole("dialog", { name: "Add to a note" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Add to note" }));
    await waitFor(() => expect(updateObservation).toHaveBeenCalledTimes(2));
    expect(updateObservation).toHaveBeenNthCalledWith(2, "note-1", expect.objectContaining({ body: "**Useful** context\n\nwhoami\n", expectedRevision: 1 }));
  });

  it("recognizes a saved append after its response is lost and does not add it twice", async () => {
    const user = userEvent.setup();
    let durable = note;
    const listObservations = vi.fn().mockImplementation(async () => ({ items: [durable], total: 1 }));
    const updateObservation = vi.fn().mockImplementationOnce(async (_id, request) => {
      durable = { ...note, body: request.body, metadata: request.metadata, revision: 2 };
      throw new Error("Response lost");
    });
    render(<DialogProvider><NotesPanel api={{ listObservations, updateObservation } as unknown as ApiClient}
      engagementId="eng-1" initialDraft={selectionDraft} /></DialogProvider>);
    await user.click(await screen.findByRole("radio", { name: "Initial note" }));
    await user.click(screen.getByRole("button", { name: "Add to note" }));
    expect(await screen.findByText("Response lost")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Add to note" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add to a note" })).not.toBeInTheDocument());
    expect(updateObservation).toHaveBeenCalledOnce();
    expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue("**Useful** context\n\nwhoami\n");
  });

  it("cancels selected-text capture without writing a note", async () => {
    const user = userEvent.setup();
    const createObservation = vi.fn();
    const updateObservation = vi.fn();
    const consumed = vi.fn();
    render(<DialogProvider><NotesPanel api={{ listObservations: vi.fn().mockResolvedValue({ items: [note], total: 1 }), createObservation, updateObservation } as unknown as ApiClient}
      engagementId="eng-1" initialDraft={selectionDraft} onInitialDraftConsumed={consumed} /></DialogProvider>);
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(consumed).toHaveBeenCalledOnce();
    expect(createObservation).not.toHaveBeenCalled();
    expect(updateObservation).not.toHaveBeenCalled();
  });
});
