import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { DialogProvider } from "./components/DialogSystem";
import { ThemeProvider } from "./state/ThemeContext";
import { WorkspaceProvider } from "./state/WorkspaceContext";

// Sending from the composer to a cloud provider: the operator is asked to
// share tool results exactly when Core would refuse without consent, and a
// send that stops before Core accepts it says why and keeps the message.

vi.mock("./components/CodeMirrorSurface", () => ({
  CodeMirrorSurface: ({ value, onChange }: { value: string; onChange(value: string): void }) => (
    <textarea aria-label="Code editor" value={value} onChange={(event) => onChange(event.target.value)} />
  ),
}));

const entity = { created_at: "2026-09-25T10:00:00Z", updated_at: "2026-09-25T11:00:00Z", revision: 1 };
const project = { ...entity, id: "engagement-1", name: "Delegation", description: "", status: "active", tags: [], metadata: {} };
const provider = {
  ...entity,
  id: "provider-1",
  name: "Cloud analyst",
  provider_type: "openrouter",
  endpoint: "https://openrouter.ai/api/v1",
  enabled: true,
  is_local: false,
  secret_ref: "env:OPEN_ROUTER_API_KEY",
  model_allowlist: ["model-1"],
  capabilities: { streaming: true, tool_calling: true },
  privacy: { local_only: false, residency: [], permits_sensitive_data: true },
  metadata: { default_model: "model-1" },
  capability_verifications: {
    "model-1": { model: "model-1", status: "verified", checked_at: "2026-09-25T10:00:00Z", contract_version: "required-tool-v1" },
  },
};
const session = (allowSubagents: boolean) => ({
  ...entity,
  id: "session-1",
  engagement_id: "engagement-1",
  title: "Delegating chat",
  backend: "provider",
  provider_profile_id: "provider-1",
  model: "model-1",
  metadata: { message_count: 0, allow_subagents: allowSubagents },
});

type Wire = Record<string, unknown>;
type Route = (path: string, init: RequestInit | undefined) => Response | Promise<Response> | undefined;

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}

function answered() {
  const encoder = new TextEncoder();
  const frames = [
    { type: "started", provider_id: "provider-1", model: "model-1", session_id: "session-1", turn_id: "turn-1" },
    { type: "done", turn_id: "turn-1", session_id: "session-1", provider_id: "provider-1", model: "model-1", message: { role: "assistant", content: "Delegated." }, usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }, finish_reason: "stop", citations: [] },
  ];
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(`event: ${frame.type}\ndata: ${JSON.stringify(frame)}\n\n`));
      controller.close();
    },
  }), { status: 200, headers: { "content-type": "text/event-stream" } });
}

function core(route: Route, sessions: Wire[]) {
  const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
    const path = new URL(String(input)).pathname;
    const routed = await route(path, init);
    if (routed) return routed;
    if (path.endsWith("/credentials/env%3AOPEN_ROUTER_API_KEY/status")) {
      return json({ reference: "env:OPEN_ROUTER_API_KEY", persistence: "environment", available: true, state: "available" });
    }
    if (path.endsWith("/providers/provider-1/health")) return json({ provider_id: "provider-1", healthy: true, models: ["model-1"], detail: null });
    if (path.endsWith("/health")) return json({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable" });
    if (path.endsWith("/engagements")) return json([project]);
    if (path.endsWith("/providers")) return json([provider]);
    if (path.endsWith("/harnesses")) return json([]);
    if (path.endsWith("/chat-sessions")) return json(sessions);
    if (path.endsWith("/pending-turn")) return json(null);
    if (path.endsWith("/goal")) return json({ detail: "Goal not found" }, 404);
    return json([]);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function completions(fetchMock: ReturnType<typeof core>) {
  return fetchMock.mock.calls
    .filter(([input, init]) => new URL(String(input)).pathname.endsWith("/chat/completions") && init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init?.body)) as Wire);
}

async function send(user: ReturnType<typeof userEvent.setup>, fetchMock: ReturnType<typeof core>, text: string) {
  render(
    <MemoryRouter initialEntries={["/sessions?view=chat&session=session-1"]}>
      <ThemeProvider><WorkspaceProvider><DialogProvider><App /></DialogProvider></WorkspaceProvider></ThemeProvider>
    </MemoryRouter>,
  );
  const composer = await screen.findByRole("textbox", { name: "Message the analyst assistant" }, { timeout: 5_000 });
  await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => new URL(String(input)).pathname.endsWith("/chat/sessions/session-1/context"))).toBe(true));
  await waitFor(() => expect(composer).toBeEnabled());
  await user.type(composer, text);
  await user.click(screen.getByRole("button", { name: "Send message" }));
  return composer;
}

/** The composer's recovery notice, where a refused send explains itself. */
function notice() {
  return waitFor(() => {
    const element = document.querySelector<HTMLElement>(".chat-recovery-notice [role='alert']");
    expect(element).not.toBeNull();
    return element!;
  });
}

describe("sending to a cloud provider", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  afterEach(() => vi.unstubAllGlobals());

  it("asks to share tool results for a Subagents turn with no command runtime, then sends with consent", { timeout: 20_000 }, async () => {
    const fetchMock = core((path, init) => path.endsWith("/chat/completions") && init?.method === "POST" ? answered() : undefined, [session(true)]);
    const user = userEvent.setup();
    await send(user, fetchMock, "Split the review");

    const dialog = await screen.findByRole("dialog", { name: "Share redacted tool results?" });
    expect(completions(fetchMock)).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Allow this turn" }));

    await waitFor(() => expect(completions(fetchMock)).toHaveLength(1));
    expect(completions(fetchMock)[0]).toMatchObject({ allow_subagents: true, allow_cloud_tool_results: true, tools_enabled: false });
    expect(await screen.findByText("Delegated.")).toBeInTheDocument();
  });

  it("explains a declined share in place and keeps the unsent message", { timeout: 20_000 }, async () => {
    const fetchMock = core(() => undefined, [session(true)]);
    const user = userEvent.setup();
    const composer = await send(user, fetchMock, "Split the review");

    await user.click(within(await screen.findByRole("dialog", { name: "Share redacted tool results?" })).getByRole("button", { name: "Cancel" }));

    expect(await notice()).toHaveTextContent("Not sent; your message is kept. Subagents would share tool results with Cloud analyst. Send again to allow sharing, or turn it off.");
    expect(composer).toHaveValue("Split the review");
    expect(completions(fetchMock)).toHaveLength(0);
  });

  it("does not ask for a plain chat on the same provider", { timeout: 20_000 }, async () => {
    const fetchMock = core((path, init) => path.endsWith("/chat/completions") && init?.method === "POST" ? answered() : undefined, [session(false)]);
    const user = userEvent.setup();
    await send(user, fetchMock, "Just answer");

    await waitFor(() => expect(completions(fetchMock)).toHaveLength(1));
    expect(screen.queryByRole("dialog", { name: "Share redacted tool results?" })).not.toBeInTheDocument();
    expect(completions(fetchMock)[0]).toMatchObject({ allow_cloud_tool_results: false });
    expect(completions(fetchMock)[0]).not.toHaveProperty("allow_subagents");
  });

  it("asks when Core requires consent for tools only it can see, and resends the same turn", { timeout: 20_000 }, async () => {
    let refused = false;
    const fetchMock = core((path, init) => {
      if (!path.endsWith("/chat/completions") || init?.method !== "POST") return undefined;
      if (!refused) {
        refused = true;
        return json({
          detail: "cloud command-result transfer requires explicit confirmation for this turn",
          code: "tool_result_consent_required",
          operator_detail: "This turn uses web search, whose tool inputs and results would go to Cloud analyst.",
          error_id: "err_consent",
          retryable: false,
        }, 409);
      }
      return answered();
    }, [session(false)]);
    const user = userEvent.setup();
    await send(user, fetchMock, "Search for the advisory");

    await user.click(within(await screen.findByRole("dialog", { name: "Share redacted tool results?" })).getByRole("button", { name: "Allow this turn" }));
    await waitFor(() => expect(completions(fetchMock)).toHaveLength(2));
    expect(completions(fetchMock)[0]).toMatchObject({ allow_cloud_tool_results: false });
    expect(completions(fetchMock)[1]).toMatchObject({ allow_cloud_tool_results: true });
    expect(await screen.findByText("Delegated.")).toBeInTheDocument();
  });

  it("returns a message Core refused for consent to the composer when sharing is declined", { timeout: 20_000 }, async () => {
    const fetchMock = core((path, init) => path.endsWith("/chat/completions") && init?.method === "POST"
      ? json({
        detail: "cloud command-result transfer requires explicit confirmation for this turn",
        code: "tool_result_consent_required",
        operator_detail: "This turn uses web search, whose tool inputs and results would go to Cloud analyst.",
        error_id: "err_consent",
      }, 409)
      : undefined, [session(false)]);
    const user = userEvent.setup();
    const composer = await send(user, fetchMock, "Search for the advisory");

    await user.click(within(await screen.findByRole("dialog", { name: "Share redacted tool results?" })).getByRole("button", { name: "Cancel" }));

    expect(await notice()).toHaveTextContent(
      "Not sent; your message is kept. This turn uses web search, whose tool inputs and results would go to Cloud analyst. Send again to allow sharing.",
    );
    await waitFor(() => expect(composer).toHaveValue("Search for the advisory"));
    // Nothing was accepted, so the transcript does not show a failed turn.
    expect(document.querySelector(".chat-message.user")).toBeNull();
    expect(completions(fetchMock)).toHaveLength(1);
  });

  it("keeps the message and names the next action for a credential state it does not know", { timeout: 20_000 }, async () => {
    const fetchMock = core((path) => path.endsWith("/credentials/env%3AOPEN_ROUTER_API_KEY/status")
      ? json({ reference: "env:OPEN_ROUTER_API_KEY", persistence: "environment", available: false, state: "rotating" })
      : undefined, [session(false)]);
    const user = userEvent.setup();
    const composer = await send(user, fetchMock, "Just answer");

    expect(await notice()).toHaveTextContent(
      'Not sent; your message is kept. Nebula Core reported an unknown credential state "rotating". Check this provider in Settings, then send again.',
    );
    expect(composer).toHaveValue("Just answer");
    expect(completions(fetchMock)).toHaveLength(0);
  });
});
