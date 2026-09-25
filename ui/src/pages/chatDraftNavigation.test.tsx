import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { App } from "../App";
import { DialogProvider } from "../components/DialogSystem";
import { ThemeProvider } from "../state/ThemeContext";
import { WorkspaceProvider } from "../state/WorkspaceContext";

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="router-location" hidden>{`${location.pathname}${location.search}`}</output>;
}

function renderApp() {
  return render(
    <MemoryRouter initialEntries={["/sessions"]}>
      <LocationProbe />
      <ThemeProvider>
        <WorkspaceProvider>
          <DialogProvider><App /></DialogProvider>
        </WorkspaceProvider>
      </ThemeProvider>
    </MemoryRouter>,
  );
}

function unmatchedCoreResponse(input: RequestInfo | URL) {
  const path = new URL(String(input)).pathname;
  if (path.endsWith("/pending-turn")) return new Response("null", { status: 200 });
  if (path.endsWith("/goal")) return new Response(JSON.stringify({ detail: "Goal not found" }), { status: 404 });
  return new Response("[]", { status: 200 });
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// A full App render, the lazy Workbench and typed input take 3-5 s under load,
// against vitest's 5 s default (CI passes --testTimeout=15000).
it("restores an unsent new-chat draft after visiting a saved conversation", async () => {
  const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
  const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
    if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Draft review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
    if (path.endsWith("/providers")) return new Response(JSON.stringify([{ ...entity, id: "provider-1", name: "Local analyst", provider_type: "vllm", endpoint: null, enabled: true, is_local: true, secret_ref: null, model_allowlist: ["model-1"], capabilities: { streaming: true }, privacy: { local_only: true, residency: [], permits_sensitive_data: false }, metadata: { default_model: "model-1" } }]), { status: 200 });
    if (path.endsWith("/chat-sessions")) return new Response(JSON.stringify([{ ...entity, id: "session-1", engagement_id: "engagement-1", title: "Saved conversation", provider_profile_id: "provider-1", model: "model-1", metadata: { message_count: 1 } }]), { status: 200 });
    if (path.endsWith("/chat/sessions/session-1/messages")) return new Response(JSON.stringify([{ ...entity, id: "message-1", engagement_id: "engagement-1", session_id: "session-1", sequence: 1, role: "assistant", content: "Saved reply", citations: [], usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } }]), { status: 200 });
    return unmatchedCoreResponse(input);
  });
  vi.stubGlobal("fetch", fetchMock);

  const storage = Object.getPrototypeOf(sessionStorage) as Storage;
  const originalGetItem = storage.getItem;
  const originalSetItem = storage.setItem;
  const originalRemoveItem = storage.removeItem;
  const isChatDraft = (key: string) => key.startsWith("nebula.assistant.draft.v1:");
  vi.spyOn(storage, "getItem").mockImplementation(function(this: Storage, key: string) {
    if (isChatDraft(key)) throw new DOMException("Storage blocked", "SecurityError");
    return originalGetItem.call(this, key);
  });
  vi.spyOn(storage, "setItem").mockImplementation(function(this: Storage, key: string, value: string) {
    if (isChatDraft(key)) throw new DOMException("Storage blocked", "SecurityError");
    return originalSetItem.call(this, key, value);
  });
  vi.spyOn(storage, "removeItem").mockImplementation(function(this: Storage, key: string) {
    if (isChatDraft(key)) throw new DOMException("Storage blocked", "SecurityError");
    return originalRemoveItem.call(this, key);
  });

  const user = userEvent.setup();
  renderApp();
  await user.click(await screen.findByRole("tab", { name: /Analyst chat/ }, { timeout: 5_000 }));
  await user.click(screen.getByRole("button", { name: "Show conversations" }));
  const conversations = await screen.findByLabelText("Conversations");
  await user.click(within(conversations).getByRole("button", { name: /New chat/ }));
  const composer = screen.getByRole("textbox", { name: "Message the analyst assistant" });
  await user.type(composer, "unsent new-chat draft");
  await user.click(within(conversations).getByTitle("Saved conversation").closest("button")!);
  await expect.poll(() => new URLSearchParams(screen.getByTestId("router-location").textContent?.split("?")[1]).get("session")).toBe("session-1");
  await user.click(within(conversations).getByRole("button", { name: /New chat/ }));

  await waitFor(() => expect(composer).toHaveValue("unsent new-chat draft"));
  expect(fetchMock.mock.calls.some(([input]) => new URL(String(input)).pathname.endsWith("/chat/completions"))).toBe(false);
}, 15_000);
