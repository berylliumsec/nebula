import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { App } from "../App";
import { DialogProvider } from "../components/DialogSystem";
import { ThemeProvider } from "../state/ThemeContext";
import { WorkspaceProvider } from "../state/WorkspaceContext";
// Exercise the navigation race after the lazy route has loaded. Cold Vite
// transforms otherwise compete with the query timeout.
import "./SessionsPage";

const originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  vi.stubGlobal("matchMedia", vi.fn().mockImplementation((query: string) => ({
    matches: query === "(max-width: 760px)" || query === "(max-width: 1100px)",
    media: query,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })));
});

afterEach(() => {
  if (originalScrollIntoView) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScrollIntoView);
  else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("keeps an operator-opened mobile drawer through delayed URL chat restoration", async () => {
  const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
  const session = {
    ...entity, id: "session-1", engagement_id: "engagement-1", title: "Priority fixture",
    provider_profile_id: "provider-1", model: "model-1",
    metadata: { message_count: 1, pinned: true, flag_color: "blue" },
  };
  let releaseSessions!: () => void;
  const sessionsReady = new Promise<void>(resolve => { releaseSessions = resolve; });
  const fetchMock = vi.fn<typeof fetch>().mockImplementation(async input => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable" }), { status: 200 });
    if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Drawer review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
    if (path.endsWith("/providers")) return new Response(JSON.stringify([{ ...entity, id: "provider-1", name: "Local analyst", provider_type: "vllm", endpoint: null, enabled: true, is_local: true, secret_ref: null, model_allowlist: ["model-1"], capabilities: { streaming: true }, privacy: { local_only: true, residency: [], permits_sensitive_data: false }, metadata: { default_model: "model-1" } }]), { status: 200 });
    if (path.endsWith("/chat-sessions")) { await sessionsReady; return new Response(JSON.stringify([session]), { status: 200 }); }
    if (path.endsWith("/chat-sessions/session-1")) { await sessionsReady; return new Response(JSON.stringify(session), { status: 200 }); }
    if (path.endsWith("/chat/sessions/session-1/task-plans")) return new Response(JSON.stringify({ session_id: "session-1", plans: [], directives: [] }), { status: 200 });
    if (path.endsWith("/chat/sessions/session-1/pending-turn")) return new Response("null", { status: 200 });
    if (path.endsWith("/chat/sessions/session-1/goal")) return new Response(JSON.stringify({ detail: "Goal not found" }), { status: 404 });
    return new Response("[]", { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/projects/engagement-1/workbench?view=chat&session=session-1"]}>
      <ThemeProvider><WorkspaceProvider><DialogProvider><App /></DialogProvider></WorkspaceProvider></ThemeProvider>
    </MemoryRouter>,
  );

  await user.click(await screen.findByRole("button", { name: "Open conversations" }, { timeout: 10_000 }));
  const drawer = screen.getByRole("complementary", { name: "Conversations" });
  expect(drawer).toHaveTextContent("0 saved");
  act(() => releaseSessions());
  const priority = (await within(drawer).findByTitle("Priority fixture")).closest("button");
  expect(priority).not.toBeNull();
  await waitFor(() => expect(priority?.closest(".session-list-item")).toHaveClass("active"));
  expect(drawer).toBeVisible();
  await user.click(priority!);
  await waitFor(() => expect(screen.queryByRole("complementary", { name: "Conversations" })).not.toBeInTheDocument());
}, 20_000);
