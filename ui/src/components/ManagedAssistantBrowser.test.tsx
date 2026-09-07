import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ManagedAssistantBrowser } from "./ManagedAssistantBrowser";

function fixture(active = true, fail = false) {
  const connection = { close: vi.fn(), send: vi.fn(), readyState: 1 };
  const request = vi.fn(async (path: string) => {
    if (fail) throw new Error("Managed Chromium is unavailable. Prepare the host and retry.");
    if (path.endsWith("/browser-companion")) return { session_id: "browser-1", conversation_id: "chat-1", tabs: [{ id: "tab-1", url: "https://example.test/", title: "Example" }] };
    if (path.endsWith("/actions")) return [];
    if (path.endsWith("/operations")) return { url: "https://example.test/?token=private-url-token", title: "Example", text: "Selected page content", page_revision: "revision-1", captured_at: "2026-09-07T12:00:00Z", structure: { tag: "main", role: "main" }, elements: [] };
    return {};
  });
  const onContext = vi.fn(); const onConversation = vi.fn();
  const api = { request, openBrowserCompanionStream: vi.fn(() => connection) } as unknown as ApiClient;
  render(<ManagedAssistantBrowser api={api} projectId="project-1" active={active} onConversation={onConversation} onContext={onContext} onImage={vi.fn()} imageSupported={false} />);
  return { request, onContext, onConversation, connection };
}

describe("ManagedAssistantBrowser", () => {
  it("does not start a browser when the operator is using another workspace view", () => {
    const { request } = fixture(false);
    expect(request).not.toHaveBeenCalled();
  });
  it("restores the durable conversation and previews context before attaching", async () => {
    const { onContext, onConversation } = fixture();
    await waitFor(() => expect(onConversation).toHaveBeenCalledWith("chat-1"));
    fireEvent.click(screen.getByRole("button", { name: "Ask about page" }));
    await screen.findByText("Selected page content");
    expect(onContext).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Attach to Assistant" }));
    expect(onContext).toHaveBeenCalledWith(expect.objectContaining({ sourceKind: "browser_companion", sourceId: "browser-1", text: expect.stringContaining("UNTRUSTED BROWSER CONTENT") }));
    expect(onContext.mock.calls[0][0].text).toContain("Page revision: revision-1");
    expect(onContext.mock.calls[0][0].text).toContain('Element structure: {"tag":"main","role":"main"}');
    expect(JSON.stringify(onContext.mock.calls)).not.toContain("private-url-token");
  });
  it("discards page context and explains unavailable image input", async () => {
    fixture();
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask about page" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Select region" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Ask about page" }));
    await screen.findByText("Selected page content");
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.queryByText("Selected page content")).not.toBeInTheDocument();
  });
  it("explains runtime failure inside the browser and offers retry", async () => {
    const { request } = fixture(true, true);
    await screen.findByRole("alert");
    expect(screen.getByRole("alert")).toHaveTextContent("Prepare the host and retry");
    fireEvent.click(screen.getByRole("button", { name: "Prepare / retry" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  });
});
