import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ManagedAssistantBrowser } from "./ManagedAssistantBrowser";

function fixture(active = true, fail = false, protectedField = false, fileField = false) {
  let savedFiles: { reference: string; filename: string; size: number; media_type: string }[] = [];
  let actions: { id: string; status: string; expires_at: string; operator_requested: boolean; request: Record<string, unknown> }[] = [];
  let savedCredentials: { reference: string; label: string; available: boolean }[] = [];
  let liveTab = { id: "tab-1", url: "https://example.test/", title: "Example" };
  const connection = { close: vi.fn(), send: vi.fn(), readyState: 1 };
  const request = vi.fn(async (path: string, options?: RequestInit) => {
    if (fail) throw new Error("Managed Chromium is unavailable. Prepare the host and retry.");
    if (path.endsWith("/browser-companion")) return { session_id: "browser-1", conversation_id: "chat-1", tabs: [{ id: "tab-1", url: "https://example.test/", title: "Example" }] };
    if (path.endsWith("/files")) {
      if (options?.method === "POST") savedFiles = [{ reference: "file-1", filename: JSON.parse(String(options.body)).filename, size: 12, media_type: "text/plain" }];
      return savedFiles;
    }
    if (path.endsWith("/files/file-1") && options?.method === "DELETE") { savedFiles = []; return savedFiles; }
    if (path.endsWith("/actions")) {
      if (options?.method === "POST") actions = [{ id: "action-1", status: "pending", expires_at: "2099-01-01T00:00:00Z", operator_requested: true, request: JSON.parse(String(options.body)) }];
      return options?.method === "POST" ? actions[0] : actions;
    }
    if (path.endsWith("/actions/action-1")) { actions[0] = { ...actions[0], status: "complete" }; return actions[0]; }
    if (path.endsWith("/operations") && JSON.parse(String(options?.body)).operation === "tabs") return { tabs: [liveTab] };
    if (path.endsWith("/credentials")) {
      if (options?.method === "POST") savedCredentials = [{ reference: "protected-1", label: JSON.parse(String(options.body)).label, available: true }];
      return savedCredentials;
    }
    if (path.endsWith("/operations")) return { url: "https://example.test/?token=private-url-token", title: "Example", text: "Selected page content", page_revision: "revision-1", captured_at: "2026-09-07T12:00:00Z", structure: { tag: "main", role: "main" }, elements: protectedField ? [{ id: "0", tag: "input", type: "password", label: "Password", sensitive: true }] : fileField ? [{ id: "2", tag: "input", type: "file", label: "Document", sensitive: false }] : [] };
    return {};
  });
  const onContext = vi.fn(); const onConversation = vi.fn();
  const onControlChange = vi.fn();
  const api = { request, openBrowserCompanionStream: vi.fn(() => connection) } as unknown as ApiClient;
  render(<ManagedAssistantBrowser api={api} projectId="project-1" active={active} onConversation={onConversation} onContext={onContext} onControlChange={onControlChange} onImage={vi.fn()} imageSupported={false} />);
  return { request, onContext, onConversation, connection, onControlChange, navigate: (url: string) => { liveTab = { ...liveTab, url, title: "Navigated page" }; } };
}

describe("ManagedAssistantBrowser", () => {
  it("refreshes live tab metadata without replacing historical context or unsent addresses", async () => {
    const { navigate, connection } = fixture();
    await waitFor(() => expect(screen.getByLabelText("Browser address")).toHaveValue("https://example.test/"));
    fireEvent.click(screen.getByRole("button", { name: "Ask about page" }));
    await screen.findByText("Selected page content");
    navigate("https://example.test/next");
    await waitFor(() => expect(screen.getByLabelText("Browser address")).toHaveValue("https://example.test/next"), { timeout: 4500 });
    expect(screen.getByRole("region", { name: "Browser context preview" })).toHaveTextContent("Selected page content");
    expect(connection.close).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Browser address"), { target: { value: "https://unsent.test/" } });
    navigate("https://example.test/again");
    await waitFor(() => expect(screen.getByRole("option", { name: "Navigated page" })).toBeInTheDocument());
    await new Promise(resolve => setTimeout(resolve, 3100));
    expect(screen.getByLabelText("Browser address")).toHaveValue("https://unsent.test/");
  }, 10000);
  it("stages a device file and requires inline approval before the page receives it", async () => {
    const { request } = fixture(true, false, false, true);
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask about page" })).toBeEnabled());
    fireEvent.click(screen.getByText("Files for this page (0)"));
    const file = new File(["file fixture"], "sample.txt", { type: "text/plain" });
    Object.defineProperty(file, "arrayBuffer", { value: async () => new TextEncoder().encode("file fixture").buffer });
    fireEvent.change(screen.getByLabelText("Attach file for page upload"), { target: { files: [file] } });
    await screen.findByRole("option", { name: "sample.txt · 12 bytes" });
    fireEvent.click(screen.getByRole("button", { name: "Ask about page" }));
    await screen.findByText("Selected page content");
    fireEvent.click(screen.getByText("Accessible page controls (1)"));
    fireEvent.click(screen.getByRole("button", { name: "Upload selected file" }));
    await screen.findByRole("region", { name: "Browser action approval" });
    expect(screen.getByRole("region", { name: "Browser action approval" })).toHaveTextContent("sample.txt");
    expect(request.mock.calls.some(([path]) => path.endsWith("/actions/action-1"))).toBe(false);
    const proposed = request.mock.calls.find(([path, options]) => path.endsWith("/actions") && options?.method === "POST");
    expect(JSON.parse(String(proposed?.[1]?.body))).toMatchObject({ operation: "upload", file_ref: "file-1", page_revision: "revision-1", element_id: "2" });
    expect(JSON.stringify(proposed)).not.toContain("ZmlsZSBmaXh0dXJl");
    fireEvent.click(screen.getByRole("button", { name: "Approve action" }));
    await waitFor(() => expect(request.mock.calls.some(([path]) => path.endsWith("/actions/action-1"))).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Remove sample.txt" }));
    await waitFor(() => expect(screen.queryByRole("option", { name: "sample.txt · 12 bytes" })).not.toBeInTheDocument());
  });
  it("saves and selects a protected value without placing its text in fill actions or Assistant context", async () => {
    const { request, onContext } = fixture(true, false, true);
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask about page" })).toBeEnabled());
    fireEvent.click(screen.getByText("Protected values (0)"));
    fireEvent.change(screen.getByLabelText("Value label"), { target: { value: "Account password" } });
    fireEvent.change(screen.getByLabelText("Private value"), { target: { value: "fixture-private-value" } });
    fireEvent.click(screen.getByRole("button", { name: "Save protected value" }));
    await screen.findByRole("option", { name: "Account password" });
    expect(screen.getByLabelText("Private value")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "Ask about page" }));
    await screen.findByText("Selected page content");
    fireEvent.click(screen.getByText("Accessible page controls (1)"));
    fireEvent.click(screen.getByRole("button", { name: "Fill protected" }));
    await waitFor(() => expect(request.mock.calls.some(([path, options]) => path.endsWith("/operations") && String(options?.body).includes('"credential_ref":"protected-1"'))).toBe(true));
    const operations = request.mock.calls.filter(([path]) => path.endsWith("/operations"));
    expect(JSON.stringify(operations)).not.toContain("fixture-private-value");
    fireEvent.click(screen.getByRole("button", { name: "Attach to Assistant" }));
    expect(JSON.stringify(onContext.mock.calls)).not.toContain("fixture-private-value");
  });
  it("reports resumed browser control for runtime privacy consent without a command runtime", async () => {
    const { onControlChange } = fixture();
    await waitFor(() => expect(screen.getByRole("button", { name: "Resume assistant control" })).toBeEnabled());
    expect(onControlChange).toHaveBeenLastCalledWith(false);
    fireEvent.click(screen.getByRole("button", { name: "Resume assistant control" }));
    await waitFor(() => expect(onControlChange).toHaveBeenLastCalledWith(true));
    fireEvent.click(screen.getByRole("button", { name: "Take control" }));
    await waitFor(() => expect(onControlChange).toHaveBeenLastCalledWith(false));
  });
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
