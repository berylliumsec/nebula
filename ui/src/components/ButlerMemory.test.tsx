import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ButlerMemory } from "./ButlerMemory";

const preference = { id: "preference-1", scope: "global_preference", engagement_id: null,
  content: "Prefer concise progress summaries", source_label: "Saved by Butler", source_ref: null,
  created_at: "2026-10-15T12:00:00Z", updated_at: "2026-10-15T13:00:00Z", revision: 3 };
const lesson = { id: "lesson-1", scope: "project_lesson", engagement_id: "project-a",
  content: "Recheck parser length before publishing", source_label: "Parser chat", source_ref: "chat-1",
  created_at: "2026-10-15T12:00:00Z", updated_at: "2026-10-15T13:00:00Z", revision: 1 };
const ready = { state: "vector_ready", detail: "" };
const response = (items: unknown[] = [preference, lesson], state = ready) => ({ items, index_status: state });
const client = (request: ReturnType<typeof vi.fn>) => ({ request } as unknown as ApiClient);

describe("ButlerMemory", () => {
  it("loads only when opened and keeps full content and source behind disclosure", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValue(response());
    render(<ButlerMemory api={client(request)} projectId="project-a" coreState="online" />);
    expect(request).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Memory" }));
    expect(await screen.findByText("2 saved memories")).toBeVisible();
    expect(screen.getByText("Prefer concise progress summaries")).toBeVisible();
    expect(screen.getByText(/Preference · Saved by Butler/)).toBeVisible();
    const projectRow = screen.getByText("Recheck parser length before publishing").closest("li")!;
    expect(within(projectRow).getByText("chat-1")).not.toBeVisible();
    await user.click(within(projectRow).getByText("Recheck parser length before publishing"));
    expect(within(projectRow).getByText("chat-1")).toBeVisible();
    expect(request).toHaveBeenCalledWith("butler/memory?engagement_id=project-a&limit=50", { signal: expect.any(AbortSignal) });
  });

  it("searches within the selected project and labels keyword fallback", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValueOnce(response()).mockResolvedValueOnce(response([lesson], { state: "lexical_fallback", detail: "Index unavailable" }));
    render(<ButlerMemory api={client(request)} projectId="project-a" coreState="online" />);
    await user.click(screen.getByRole("button", { name: "Memory" }));
    await screen.findByText("2 saved memories");
    await user.type(screen.getByRole("searchbox", { name: "Search Butler memory" }), "parser");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("1 search result")).toBeVisible();
    expect(screen.getByText(/Search is using keyword matching/)).toBeVisible();
    expect(request).toHaveBeenLastCalledWith("butler/memory/search", { method: "POST",
      body: JSON.stringify({ engagement_id: "project-a", query: "parser", limit: 50 }), signal: expect.any(AbortSignal) });
  });

  it("edits with the saved revision and reloads the authoritative item", async () => {
    const user = userEvent.setup();
    const updated = { ...preference, content: "Prefer short status summaries", revision: 4 };
    const request = vi.fn().mockResolvedValueOnce(response([preference])).mockResolvedValueOnce(updated).mockResolvedValueOnce(response([updated]));
    render(<ButlerMemory api={client(request)} projectId="project-a" coreState="online" />);
    await user.click(screen.getByRole("button", { name: "Memory" }));
    await user.click(await screen.findByText("Prefer concise progress summaries"));
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const editor = screen.getByRole("textbox", { name: "Memory content" });
    await user.clear(editor);
    await user.type(editor, "Prefer short status summaries");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Prefer short status summaries")).toBeVisible();
    expect(request).toHaveBeenCalledWith("butler/memory/preference-1", { method: "PATCH",
      body: JSON.stringify({ engagement_id: "project-a", content: "Prefer short status summaries", expected_revision: 3 }) });
    expect(screen.getByText("Memory updated.")).toBeVisible();
  });

  it("requires an explicit delete confirmation and reloads the list", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValueOnce(response([lesson])).mockResolvedValueOnce(undefined).mockResolvedValueOnce(response([]));
    render(<ButlerMemory api={client(request)} projectId="project-a" coreState="online" />);
    await user.click(screen.getByRole("button", { name: "Memory" }));
    await user.click(await screen.findByText("Recheck parser length before publishing"));
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(request).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Delete this memory? Butler will no longer use it.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Delete memory" }));
    await waitFor(() => expect(screen.getByText("0 saved memories")).toBeVisible());
    expect(request).toHaveBeenCalledWith("butler/memory/lesson-1?engagement_id=project-a&expected_revision=1", { method: "DELETE" });
    expect(screen.getByText("Memory deleted.")).toBeVisible();
  });

  it("drops the previous project's memory immediately on project switch", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValueOnce(response([lesson])).mockResolvedValueOnce(response([]));
    const api = client(request);
    const view = render(<ButlerMemory api={api} projectId="project-a" coreState="online" />);
    await user.click(screen.getByRole("button", { name: "Memory" }));
    expect(await screen.findByText("Recheck parser length before publishing")).toBeVisible();
    view.rerender(<ButlerMemory api={api} projectId="project-b" coreState="online" />);
    expect(screen.queryByText("Recheck parser length before publishing")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Memory" })).toHaveAttribute("aria-expanded", "false");
    await user.click(screen.getByRole("button", { name: "Memory" }));
    expect(await screen.findByText("0 saved memories")).toBeVisible();
    expect(request).toHaveBeenLastCalledWith("butler/memory?engagement_id=project-b&limit=50", { signal: expect.any(AbortSignal) });
  });

  it("offers Retry after a failed read and disables edits when Core goes offline", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockRejectedValueOnce(new Error("Connection failed")).mockResolvedValueOnce(response([preference]));
    const api = client(request);
    const view = render(<ButlerMemory api={api} projectId="project-a" coreState="online" />);
    await user.click(screen.getByRole("button", { name: "Memory" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection failed");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await user.click(await screen.findByText("Prefer concise progress summaries"));
    view.rerender(<ButlerMemory api={api} projectId="project-a" coreState="offline" />);
    expect(screen.getByText(/Core is offline/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Edit" })).toBeDisabled();
  });
});
