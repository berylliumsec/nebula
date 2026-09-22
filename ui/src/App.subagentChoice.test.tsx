import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { DialogProvider } from "./components/DialogSystem";
import { ThemeProvider } from "./state/ThemeContext";
import { WorkspaceProvider } from "./state/WorkspaceContext";

// The Subagents box is re-read from the conversation whenever the composer
// selects it or its revision changes. These journeys check it stays as the
// operator left it when that conversation is new, or when a list read that
// started before the choice was saved lands after it.

vi.mock("./components/CodeMirrorSurface", () => ({
  CodeMirrorSurface: ({ value, onChange }: { value: string; onChange(value: string): void }) => (
    <textarea aria-label="Code editor" value={value} onChange={(event) => onChange(event.target.value)} />
  ),
}));

const entity = { created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T11:00:00Z", revision: 1 };
const project = { ...entity, id: "engagement-1", name: "Delegation", description: "", status: "active", tags: [], metadata: {} };
const provider = {
  ...entity,
  id: "provider-1",
  name: "Local analyst",
  provider_type: "vllm",
  endpoint: null,
  enabled: true,
  is_local: true,
  secret_ref: null,
  model_allowlist: ["model-1"],
  capabilities: { streaming: true, tool_calling: true },
  privacy: { local_only: true, residency: [], permits_sensitive_data: true },
  metadata: { default_model: "model-1" },
};
const verified = {
  "model-1": { model: "model-1", status: "verified", checked_at: "2026-09-21T10:00:00Z", contract_version: "required-tool-v1" },
};
const harness = {
  ...entity,
  id: "harness-1",
  name: "Codex",
  kind: "codex_app_server",
  connection_mode: "spawn",
  transport: "stdio",
  auth_mode: "existing_session",
  default_model: "codex-model",
  enabled: true,
  privacy: { local_only: true, permits_sensitive_data: true },
  native_capabilities: { workspace_access: "write", shell: true },
  capabilities: { checked_at: "2026-09-21T10:00:00Z", authentication_state: "verified", models: ["codex-model"] },
};

type Wire = Record<string, unknown>;
type Route = (path: string, init: RequestInit | undefined, url: URL) => Response | Promise<Response> | undefined;

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status });
}

function stream(frames: Wire[]) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(`event: ${String(frame.type)}\ndata: ${JSON.stringify(frame)}\n\n`));
      controller.close();
    },
  }), { status: 200, headers: { "content-type": "text/event-stream" } });
}

function core(route: Route, providers: Wire[] = [{ ...provider, capability_verifications: verified }], harnesses: Wire[] = []) {
  const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
    const url = new URL(String(input));
    const path = url.pathname;
    const routed = await route(path, init, url);
    if (routed) return routed;
    if (path.endsWith("/providers/provider-1/health")) return json({ provider_id: "provider-1", healthy: true, models: ["model-1"], detail: null });
    if (path.endsWith("/health")) return json({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable" });
    if (path.endsWith("/engagements")) return json([project]);
    if (path.endsWith("/providers")) return json(providers);
    if (path.endsWith("/harnesses")) return json(harnesses);
    if (path.endsWith("/pending-turn")) return json(null);
    if (path.endsWith("/goal")) return json({ detail: "Goal not found" }, 404);
    return json([]);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function calls(fetchMock: ReturnType<typeof core>, suffix: string, method = "POST") {
  return fetchMock.mock.calls.filter(([input, init]) => new URL(String(input)).pathname.endsWith(suffix) && (init?.method ?? "GET") === method);
}

function renderApp(route: string) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <ThemeProvider><WorkspaceProvider><DialogProvider><App /></DialogProvider></WorkspaceProvider></ThemeProvider>
    </MemoryRouter>,
  );
}

async function openAssistantSettings(user: ReturnType<typeof userEvent.setup>) {
  if (!screen.queryByRole("dialog", { name: "Assistant settings" })) {
    await user.click(screen.getByRole("button", { name: "Assistant settings" }));
  }
  return screen.findByRole("dialog", { name: "Assistant settings" });
}

describe("the Subagents choice", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  afterEach(() => vi.unstubAllGlobals());

  it("stays checked on a goal conversation created before the first message", { timeout: 20_000 }, async () => {
    let sessions: Wire[] = [];
    let goal: Wire | undefined;
    const fetchMock = core((path, init) => {
      if (path.endsWith("/chat/goal-conversations") && init?.method === "POST") {
        // Core saves what the composer sends on the new conversation.
        const body = JSON.parse(String(init.body)) as Wire;
        const session = {
          ...entity,
          id: "goal-chat",
          engagement_id: "engagement-1",
          title: String(body.objective),
          backend: "provider",
          provider_profile_id: body.provider_id,
          model: body.model,
          metadata: {
            tools_enabled: body.tools_enabled,
            mcp_server_ids: body.mcp_server_ids,
            hook_ids: body.hook_ids,
            reasoning_effort: body.reasoning_effort ?? null,
            allow_subagents: body.allow_subagents ?? false,
            max_active_subagents: body.max_active_subagents ?? null,
            message_count: 0,
          },
        };
        goal = {
          ...entity,
          id: "goal-1",
          engagement_id: "engagement-1",
          session_id: "goal-chat",
          objective: body.objective,
          completion_criteria: body.completion_criteria,
          plan: [],
          current_step: 0,
          status: "draft",
          usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
          skill_snapshots: [],
        };
        sessions = [session];
        return json({ session, goal }, 201);
      }
      if (path.endsWith("/chat/sessions/goal-chat/goal") && goal) return json(goal);
      if (path.endsWith("/chat-sessions")) return json(sessions);
      return undefined;
    });
    const user = userEvent.setup();
    renderApp("/sessions");

    await user.click(await screen.findByRole("tab", { name: /Analyst chat/ }, { timeout: 5_000 }));
    await user.click(screen.getByRole("button", { name: "New chat" }));
    const settings = await openAssistantSettings(user);
    await user.click(await screen.findByRole("checkbox", { name: /^Subagents/ }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Reasoning effort" }), "high");
    expect(settings).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close assistant settings" }));

    await user.click(screen.getByRole("button", { name: "Add goal" }));
    await user.type(screen.getByLabelText("Objective"), "Split the review");
    await user.type(screen.getByLabelText("Completion criteria"), "Every area is reviewed");
    await user.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => expect(calls(fetchMock, "/chat/goal-conversations")).toHaveLength(1));
    expect(JSON.parse(String(calls(fetchMock, "/chat/goal-conversations")[0][1]?.body))).toMatchObject({
      allow_subagents: true,
      reasoning_effort: "high",
    });
    await waitFor(() => expect(calls(fetchMock, "/chat/sessions/goal-chat/context", "GET").length).toBeGreaterThan(0));
    await openAssistantSettings(user);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /^Subagents/ })).toBeChecked());
    expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveValue("high");
  });

  it("stays checked on a new harness chat while the subagent model is being verified", { timeout: 20_000 }, async () => {
    let sessions: Wire[] = [];
    const fetchMock = core((path, init) => {
      // Verification runs for the whole journey.
      if (path.endsWith("/capabilities/verify")) return new Promise<Response>(() => undefined);
      if (path.endsWith("/chat/completions") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Wire;
        const pending = body.pending_provider_subagent as Wire | undefined;
        sessions = [{
          ...entity,
          id: "harness-chat",
          engagement_id: "engagement-1",
          title: "Hello",
          backend: "harness",
          harness_profile_id: "harness-1",
          harness_session_id: "vendor-session",
          model: "codex-model",
          // Core remembers the pending choice on the new conversation.
          metadata: pending ? { provider_subagent: pending } : {},
        }];
        return stream([
          { type: "started", session_id: "harness-chat", turn_id: "turn-1", harness_profile_id: "harness-1", harness_session_id: "vendor-session", harness_turn_id: "harness-turn-1", model: "codex-model" },
          { type: "done", session_id: "harness-chat", turn_id: "turn-1", message: { role: "assistant", content: "Hi." }, citations: [] },
        ]);
      }
      if (path.endsWith("/harness-sessions/vendor-session/activity")) {
        return json({ session_id: "vendor-session", session_status: "ready", busy: false, live: true, commands: [], last_activity_at: "2026-09-21T11:00:00Z", detail: "Ready" });
      }
      if (path.endsWith("/chat-sessions")) return json(sessions);
      return undefined;
    }, [provider], [harness]);
    const user = userEvent.setup();
    renderApp("/sessions");

    await user.click(await screen.findByRole("tab", { name: /Analyst chat/ }, { timeout: 5_000 }));
    await user.click(screen.getByRole("button", { name: "New chat" }));
    await openAssistantSettings(user);
    await user.selectOptions(await screen.findByRole("combobox", { name: "Chat runtime" }), "harness");
    await user.click(await screen.findByRole("checkbox", { name: /Provider subagents/ }));
    expect(screen.getByRole("combobox", { name: "Subagent model" })).toHaveValue("model-1");
    expect(screen.getByText("Checking tool support for this model…")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close assistant settings" }));

    await user.type(screen.getByRole("textbox", { name: "Message the analyst assistant" }), "Hello");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(calls(fetchMock, "/chat/completions")).toHaveLength(1));
    const sent = JSON.parse(String(calls(fetchMock, "/chat/completions")[0][1]?.body)) as Wire;
    // The turn cannot use an unverified model, but the choice travels with it.
    expect(sent).not.toHaveProperty("allow_subagents");
    expect(sent).not.toHaveProperty("subagent_model");
    expect(sent.pending_provider_subagent).toEqual({ provider_profile_id: "provider-1", model: "model-1" });
    await waitFor(() => expect(calls(fetchMock, "/chat/sessions/harness-chat/context", "GET").length).toBeGreaterThan(0));
    await openAssistantSettings(user);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Provider subagents/ })).toBeChecked());
    expect(screen.getByRole("combobox", { name: "Subagent model" })).toHaveValue("model-1");
  });

  it("stays checked when an older conversation list lands after the choice is saved", { timeout: 20_000 }, async () => {
    const session = (revision: number, allowSubagents: boolean) => ({
      ...entity,
      revision,
      id: "session-1",
      engagement_id: "engagement-1",
      title: "Delegating chat",
      backend: "provider",
      provider_profile_id: "provider-1",
      model: "model-1",
      metadata: { message_count: revision > 1 ? 2 : 0, allow_subagents: allowSubagents },
    });
    let listed: Wire = session(1, false);
    let stale: Wire | undefined;
    let releaseStaleList: (() => void) | undefined;
    const fetchMock = core((path, init) => {
      if (path.endsWith("/chat/completions") && init?.method === "POST") {
        // The list read the started event triggers answers late, with the
        // conversation as the turn began.
        stale = session(2, false);
        listed = session(3, false);
        return stream([
          { type: "started", provider_id: "provider-1", model: "model-1", session_id: "session-1", turn_id: "turn-1" },
          { type: "done", turn_id: "turn-1", session_id: "session-1", provider_id: "provider-1", model: "model-1", message: { role: "assistant", content: "Done." }, usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 }, finish_reason: "stop", citations: [] },
        ]);
      }
      if (path.endsWith("/chat-sessions/session-1") && init?.method === "PATCH") {
        const body = JSON.parse(String(init.body)) as Wire;
        listed = session(4, body.allow_subagents === true);
        return json(listed);
      }
      if (path.endsWith("/chat-sessions")) {
        if (stale) {
          const held = stale;
          stale = undefined;
          return new Promise<Response>((resolve) => { releaseStaleList = () => resolve(json([held])); });
        }
        return json([listed]);
      }
      return undefined;
    });
    const user = userEvent.setup();
    renderApp("/sessions?view=chat&session=session-1");

    const composer = await screen.findByRole("textbox", { name: "Message the analyst assistant" }, { timeout: 5_000 });
    await waitFor(() => expect(calls(fetchMock, "/chat/sessions/session-1/context", "GET").length).toBeGreaterThan(0));
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "Look around");
    expect(composer).toHaveValue("Look around");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText("Done.")).toBeInTheDocument();
    await openAssistantSettings(user);
    const box = screen.getByRole("checkbox", { name: /^Subagents/ });
    await waitFor(() => expect(box).toBeEnabled());

    await user.click(box);
    await waitFor(() => expect(calls(fetchMock, "/chat-sessions/session-1", "PATCH")).toHaveLength(1));
    expect(await screen.findByText("Subagents saved. Applies to your next message.")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /^Subagents/ })).toBeChecked();

    expect(releaseStaleList).toBeDefined();
    releaseStaleList?.();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByRole("checkbox", { name: /^Subagents/ })).toBeChecked();
  });
});
