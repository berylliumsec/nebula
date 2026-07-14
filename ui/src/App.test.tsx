import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { DialogProvider } from "./components/DialogSystem";
import { ThemeProvider } from "./state/ThemeContext";
import { WorkspaceProvider } from "./state/WorkspaceContext";

function renderApp(route = "/") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <ThemeProvider>
        <WorkspaceProvider>
          <DialogProvider>
            <App />
          </DialogProvider>
        </WorkspaceProvider>
      </ThemeProvider>
    </MemoryRouter>,
  );
}

function selectElementText(element: HTMLElement) {
  const range = document.createRange();
  range.selectNodeContents(element);
  const selection = document.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
  fireEvent.pointerUp(element);
}

describe("Nebula workspace", () => {
  beforeEach(() => {
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Core offline")));
  });

  afterEach(() => vi.unstubAllGlobals());

  it("exposes every primary workspace destination", async () => {
    renderApp();
    for (const label of ["Workbench", "Findings", "Reports", "Project", "Settings"]) {
      expect(screen.getByRole("link", { name: label })).toBeVisible();
    }
    expect(await screen.findByRole("heading", { name: "Workbench" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "Terminal" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Autonomous missions" })).toBeVisible();
    expect(screen.queryByText(/Acme|Jordan/i)).not.toBeInTheDocument();
  });

  it("restores legacy mission links to the Workbench mission view", async () => {
    renderApp("/missions");
    expect(await screen.findByRole("heading", { name: "Workbench" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "Autonomous missions" })).toHaveAttribute("aria-selected", "true");
  });

  it("navigates with the keyboard command palette", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.keyboard("{Control>}k{/Control}");
    const search = screen.getByRole("textbox", { name: "Search commands" });
    await user.type(search, "provider");
    await user.click(screen.getByRole("option", { name: /Go to Settings/ }));
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
  });

  it("applies the accessible high-contrast preference", async () => {
    const user = userEvent.setup();
    renderApp("/settings");
    await screen.findByRole("heading", { name: "Settings" });
    await user.click(screen.getByRole("link", { name: "Advanced settings" }));
    await user.click(screen.getByRole("button", { name: /High contrast/ }));
    expect(document.documentElement).toHaveAttribute("data-theme", "high-contrast");
    expect(localStorage.getItem("nebula.theme")).toBe("high-contrast");
  });

  it("selects Zero from command search and restores the saved preference", async () => {
    const user = userEvent.setup();
    const firstRender = renderApp();
    await user.keyboard("{Control>}k{/Control}");
    await user.type(screen.getByRole("textbox", { name: "Search commands" }), "zero theme");
    await user.click(screen.getByRole("option", { name: /Use Zero theme/ }));
    expect(document.documentElement).toHaveAttribute("data-theme", "zero");
    expect(localStorage.getItem("nebula.theme")).toBe("zero");

    firstRender.unmount();
    renderApp("/settings");
    await screen.findByRole("heading", { name: "Settings" });
    await user.click(screen.getByRole("link", { name: "Advanced settings" }));
    expect(screen.getByRole("button", { name: /Zero/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("renders the contextual Zero shell only for the restored Zero preference", async () => {
    localStorage.setItem("nebula.theme", "zero");
    const firstRender = renderApp();
    expect(await screen.findByRole("region", { name: "Zero Layer context" })).toBeVisible();
    expect(document.querySelector(".app-shell")).toHaveClass("zero-layer-shell");
    expect(screen.getByRole("link", { name: /Open overview/ })).toHaveAttribute("href", "/project");

    firstRender.unmount();
    localStorage.setItem("nebula.theme", "dark");
    renderApp();
    expect(screen.queryByRole("region", { name: "Zero Layer context" })).not.toBeInTheDocument();
    expect(document.querySelector(".app-shell")).not.toHaveClass("zero-layer-shell");
  });

  it("persists the collapsible sidebar and exposes legacy labels through command search", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole("button", { name: "Hide sidebar" }));
    expect(screen.getByRole("button", { name: "Show sidebar" })).toBeVisible();
    expect(localStorage.getItem("nebula.sidebar.collapsed")).toBe("true");
    await user.keyboard("{Control>}k{/Control}");
    await user.type(screen.getByRole("textbox", { name: "Search commands" }), "Overview");
    expect(screen.getByRole("option", { name: /Go to Project/ })).toBeVisible();
  });

  it("routes browser and desktop global shortcuts through the shared shell commands", async () => {
    const user = userEvent.setup();
    renderApp("/findings");
    expect(await screen.findByRole("heading", { name: "Findings" })).toBeVisible();
    await user.keyboard("{Control>},{/Control}");
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
    await user.keyboard("{Control>}1{/Control}");
    expect(await screen.findByRole("heading", { name: "Workbench" })).toBeVisible();
  });

  it("shows a truthful Core failure without fabricated workspace records", async () => {
    renderApp();
    expect(await screen.findByRole("alert")).toHaveTextContent("Nebula Core could not start");
    expect(screen.queryByRole("button", { name: /Show activity inspector/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Acme|Jordan|Gateway applicability/i)).not.toBeInTheDocument();
  });

  it("reduces settings to setup and advanced views while preserving legacy hashes", async () => {
    window.history.replaceState({}, "", "/settings#security-settings");
    const user = userEvent.setup();
    renderApp("/settings");
    expect(await screen.findByRole("link", { name: "Advanced settings" })).toHaveAttribute("aria-current", "page");
    await user.click(screen.getByRole("link", { name: "Setup" }));
    expect(screen.getByRole("link", { name: "Setup" })).toHaveAttribute("aria-current", "page");
  });

  it("selects the newest Core run and opens its replay stream", async () => {
    class OnlineWebSocket extends EventTarget {
      static lastUrl = "";
      static instance: OnlineWebSocket | undefined;

      constructor(url: string | URL) {
        super();
        OnlineWebSocket.lastUrl = String(url);
        OnlineWebSocket.instance = this;
      }

      close() {}
    }

    const entity = {
      created_at: "2026-07-12T10:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) {
        return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable" }), { status: 200 });
      }
      if (url.pathname.endsWith("/engagements")) {
        return new Response(JSON.stringify([{ ...entity, id: "engagement-first", name: "Live engagement", status: "active", metadata: {} }]), { status: 200 });
      }
      if (url.pathname.endsWith("/runs")) {
        return new Response(JSON.stringify([
          { ...entity, id: "run-oldest", engagement_id: "engagement-first", objective: "Old mission", status: "complete", metadata: {} },
          { ...entity, id: "run-newest", engagement_id: "engagement-first", objective: "Live mission", status: "running", updated_at: "2026-07-12T12:00:00Z", metadata: {} },
        ]), { status: 200 });
      }
      if (url.pathname.endsWith("/runs/run-newest/context")) {
        return new Response(JSON.stringify({
          owner_type: "agent_run", owner_id: "run-newest", status: "ready", context_window: 8192, max_output_tokens: 2048, target_input_tokens: 4608, estimated_input_tokens: 5000, compacted_through: 4,
          snapshot: { ...entity, id: "snapshot-run", owner_type: "agent_run", owner_id: "run-newest", version: 1, status: "ready", compacted_through: 4, memory: { summary: "Dependency results retained for synthesis.", confirmed_facts: [], decisions: [], constraints: [], corrections: [], open_questions: [], evidence_ids: ["evidence-1"], artifact_ids: [] }, source_references: [{ source_kind: "task_result", source_id: "task-1" }], provider_profile_id: "provider-1", model: "model-1", prompt_version: "nebula-context-v1", usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 }, cost_usd: 0.001 },
        }), { status: 200 });
      }
      if (url.pathname.endsWith("/approvals")) {
        return new Response(JSON.stringify([{ ...entity, id: "approval-first", engagement_id: "engagement-first", run_id: "run-first", status: "pending", risk_class: "active_scan", exact_request: { tool_name: "scan.tcp", arguments: { ports: [443] } }, policy_rationale: "Active scan approval", requested_by: "network-specialist", requested_at: entity.created_at, expected_effects: ["Probe the target"] }]), { status: 200 });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", OnlineWebSocket);
    renderApp("/project");

    expect(await screen.findByRole("heading", { name: "Live engagement" })).toBeVisible();
    const emptyActivity = screen.getByText("No mission activity", { exact: true }).closest(".mission-events-empty") as HTMLElement | null;
    expect(emptyActivity).not.toBeNull();
    expect(within(emptyActivity!).getByText("Events appear after Core records a transition.", { exact: true })).toBeVisible();
    expect(emptyActivity!.closest("li")).toBeNull();
    expect(screen.getByRole("button", { name: /Show activity inspector/ })).toBeVisible();
    expect(OnlineWebSocket.lastUrl).toContain("/api/v1/runs/run-newest/events/ws?after=0");
    const approvalLoads = fetchMock.mock.calls.filter(([input]) => new URL(String(input)).pathname.endsWith("/approvals")).length;
    act(() => OnlineWebSocket.instance?.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ kind: "event", event: { id: "event-live", run_id: "run-newest", sequence: 1, event_type: "approval.requested", payload: { summary: "Approval requested" }, occurred_at: "2026-07-12T12:01:00Z" } }) })));
    expect(fetchMock.mock.calls.filter(([input]) => new URL(String(input)).pathname.endsWith("/approvals"))).toHaveLength(approvalLoads + 1);
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual(expect.arrayContaining([
      expect.stringContaining("/api/v1/runs?engagement_id=engagement-first"),
      expect.stringContaining("/api/v1/approvals?engagement_id=engagement-first"),
      expect.stringContaining("/api/v1/assets?engagement_id=engagement-first"),
      expect.stringContaining("/api/v1/findings?engagement_id=engagement-first"),
      expect.stringMatching(/\/api\/v1\/providers\?limit=1000&offset=0$/),
    ]));
  });

  it("streams analyst chat with explicit provider/model selection and cloud knowledge consent", async () => {
    const entity = {
      created_at: "2026-07-12T10:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    };
    const encoder = new TextEncoder();
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/providers/provider-1/health")) {
        return new Response(JSON.stringify({ provider_id: "provider-1", healthy: true, models: ["model-1", "model-2"], detail: null }), { status: 200 });
      }
      if (url.pathname.endsWith("/health")) {
        return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable" }), { status: 200 });
      }
      if (url.pathname.endsWith("/engagements")) {
        return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Live engagement", status: "active", metadata: {} }]), { status: 200 });
      }
      if (url.pathname.endsWith("/providers")) {
        return new Response(JSON.stringify([{
          ...entity,
          id: "provider-1",
          name: "Cloud analyst",
          provider_type: "openai",
          endpoint: "https://api.openai.com/v1",
          enabled: true,
          is_local: false,
          secret_ref: "env:OPENAI_API_KEY",
          model_allowlist: [],
          capabilities: { streaming: true },
          privacy: { local_only: false, residency: [], permits_sensitive_data: true },
          metadata: {},
        }]), { status: 200 });
      }
      if (url.pathname.endsWith("/knowledge")) {
        return new Response(JSON.stringify([{
          ...entity,
          id: "source-1",
          engagement_id: "engagement-1",
          name: "scope.md",
          source_type: "document",
          artifact_id: "artifact-1",
          status: "ready",
          citation: "scope.md",
          document_count: 1,
          metadata: { filename: "scope.md", media_type: "text/markdown", chunk_count: 1 },
        }]), { status: 200 });
      }
      if (url.pathname.endsWith("/chat/completions")) {
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode('event: started\ndata: {"type":"started","provider_id":"provider-1","model":"model-1","session_id":"session-1"}\n\n'));
            controller.enqueue(encoder.encode('event: delta\ndata: {"type":"delta","provider_id":"provider-1","model":"model-1","delta":"Bounded "}\n\n'));
            controller.enqueue(encoder.encode('event: done\ndata: {"type":"done","session_id":"session-1","provider_id":"provider-1","model":"model-1","message":{"role":"assistant","content":"Bounded answer"},"usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5},"finish_reason":"stop","provider_request_id":"request-1","citations":[]}\n\n'));
            controller.close();
          },
        });
        return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/sessions");
    await user.click(await screen.findByRole("tab", { name: /Analyst chat/ }));
    await user.click(screen.getByText("Assistant settings"));
    expect(await screen.findByRole("combobox", { name: "Chat provider" })).toHaveValue("provider-1");
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Chat model" })).toHaveValue("model-1"));
    expect(screen.getByRole("option", { name: "model-2" })).toBeVisible();
    expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname.endsWith("/providers/provider-1/health") && init?.method === "POST")).toBe(true);
    await user.type(screen.getByRole("textbox", { name: "Message the analyst assistant" }), "Review the scope");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByRole("button", { name: "Allow this request" }));

    expect(await screen.findByText("Bounded answer")).toBeVisible();
    const chatCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/api/v1/chat/completions"));
    expect(JSON.parse(String(chatCall?.[1]?.body))).toMatchObject({
      provider_id: "provider-1",
      model: "model-1",
      engagement_id: "engagement-1",
      include_knowledge: true,
      allow_cloud_knowledge: true,
      messages: [{ role: "user", content: "Review the scope" }],
    });
  });

  it("verifies the exact chat model and uses a ready Toolbox assignment automatically", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const encoder = new TextEncoder();
    let verified = false;
    const providerPayload = () => ({
      ...entity,
      revision: verified ? 2 : 1,
      id: "provider-1",
      name: "Local tools",
      provider_type: "vllm",
      enabled: true,
      is_local: true,
      model_allowlist: ["model-1", "model-2"],
      capabilities: { streaming: true, tool_calling: verified },
      capability_verifications: {
        "model-1": { model: "model-1", status: "verified", checked_at: entity.updated_at, contract_version: "required-tool-v1" },
        ...(verified ? { "model-2": { model: "model-2", status: "verified", checked_at: entity.updated_at, contract_version: "required-tool-v1" } } : {}),
      },
      privacy: { local_only: true, residency: [] },
      metadata: { default_model: "model-2" },
    });
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health") && path.includes("/providers/")) return new Response(JSON.stringify({ provider_id: "provider-1", healthy: true, models: ["model-1", "model-2"] }), { status: 200 });
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "ready", human_pty: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Automatic tools", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/providers")) return new Response(JSON.stringify([providerPayload()]), { status: 200 });
      if (path.endsWith("/capabilities/verify") && init?.method === "POST") {
        verified = true;
        return new Response(JSON.stringify({ status: "verified" }), { status: 200 });
      }
      if (path.endsWith("/providers/provider-1")) return new Response(JSON.stringify(providerPayload()), { status: 200 });
      if (path.endsWith("/tool-assignment")) return new Response(JSON.stringify([
        { id: "assignment-stale", engagement_id: "engagement-1", manifest_digest: "sha256:replaced", tool_names: ["environment.run_network"], enabled: true, revision: 1 },
        { id: "assignment-ready", engagement_id: "engagement-1", manifest_digest: "sha256:toolbox", tool_names: ["environment.run_network"], enabled: true, revision: 1 },
      ]), { status: 200 });
      if (path.endsWith("/tool-packs")) return new Response(JSON.stringify([{ id: "toolbox", publisher: "berylliumsec", name: "nebula-toolbox", version: "0.1.0", manifest_digest: "sha256:toolbox", source: "catalog", trust_state: "trusted", runtime_profile_id: "runner-1", image_locks: {}, status: "ready", tool_names: ["environment.run_network"], permissions: ["network"] }]), { status: 200 });
      if (path.endsWith("/tools")) return new Response(JSON.stringify([{ name: "environment.run_network", pack_id: "toolbox", pack_manifest_digest: "sha256:toolbox", description: "Run a network command", risk_class: "active_scan", requires_network: true, requires_approval: false, available: true }]), { status: 200 });
      if (path.endsWith("/chat/completions") && init?.method === "POST") {
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode('event: done\ndata: {"type":"done","session_id":"session-1","provider_id":"provider-1","model":"model-2","message":{"role":"assistant","content":"Tool-ready answer"},"usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5},"finish_reason":"stop","citations":[]}\n\n'));
            controller.close();
          },
        });
        return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/sessions");

    const assistantSettings = await screen.findByText("Assistant settings");
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Chat provider" })).toHaveValue("provider-1"));
    const assistantSettingsPanel = assistantSettings.closest("details");
    if (!assistantSettingsPanel?.hasAttribute("open")) await user.click(assistantSettings);
    expect(assistantSettingsPanel).toHaveAttribute("open");
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => new URL(String(input)).pathname.endsWith("/tools"))).toBe(true));
    expect(screen.getAllByText("Toolbox automatic").length).toBeGreaterThan(0);
    const verificationCall = fetchMock.mock.calls.find(([input, request]) => new URL(String(input)).pathname.endsWith("/capabilities/verify") && request?.method === "POST");
    expect(JSON.parse(String(verificationCall?.[1]?.body))).toMatchObject({ model: "model-2", expected_revision: 1 });
    expect(screen.queryByRole("checkbox", { name: /Toolbox/i })).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Message the analyst assistant" }), "Use the assigned capability");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText("Tool-ready answer")).toBeVisible();

    const chatCall = fetchMock.mock.calls.find(([input, request]) => new URL(String(input)).pathname.endsWith("/chat/completions") && request?.method === "POST");
    expect(JSON.parse(String(chatCall?.[1]?.body))).toMatchObject({ provider_id: "provider-1", model: "model-2", tools_enabled: true });
  });

  it("opens selected text as an editable draft and hashes it only on explicit send", async () => {
    const entity = {
      created_at: "2026-07-12T10:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    };
    const encoder = new TextEncoder();
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "project-1", name: "Selection review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/providers/provider-1/health") && init?.method === "POST") return new Response(JSON.stringify({ provider_id: "provider-1", healthy: true, models: ["model-1"], detail: null }), { status: 200 });
      if (path.endsWith("/providers")) return new Response(JSON.stringify([{
        ...entity,
        id: "provider-1",
        name: "Local assistant",
        provider_type: "vllm",
        endpoint: "http://127.0.0.1:8000/v1",
        enabled: true,
        is_local: true,
        secret_ref: null,
        model_allowlist: ["model-1"],
        capabilities: { streaming: true },
        privacy: { local_only: true, residency: [], permits_sensitive_data: false },
        metadata: { default_model: "model-1" },
      }]), { status: 200 });
      if (path.endsWith("/setup/status")) return new Response(JSON.stringify({
        core: { status: "ready", detail: null },
        scratch_project_id: null,
        terminal: { status: "disabled", runner_profile_id: null, candidates: [], detail: null },
        assistant: { status: "configured", provider_profile_id: "provider-1", detail: null },
      }), { status: 200 });
      if (path.endsWith("/chat/completions")) {
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode('event: done\ndata: {"type":"done","session_id":"session-selection","provider_id":"provider-1","model":"model-1","message":{"role":"assistant","content":"Selection explained"},"usage":{"input_tokens":4,"output_tokens":2,"total_tokens":6},"finish_reason":"stop","citations":[]}\n\n'));
            controller.close();
          },
        });
        return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/project");

    const heading = await screen.findByRole("heading", { name: "Selection review" });
    selectElementText(heading);
    await user.click(await screen.findByRole("button", { name: "Ask Nebula" }));

    expect(await screen.findByRole("tab", { name: "Analyst chat" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("group", { name: "Selected context attachment" })).toHaveTextContent("Selection review");
    const composer = screen.getByRole("textbox", { name: "Message the analyst assistant" });
    expect(composer).toHaveValue("");
    expect(fetchMock.mock.calls.some(([input]) => new URL(String(input)).pathname.endsWith("/chat/completions"))).toBe(false);

    await user.clear(composer);
    await user.type(composer, "Explain this project title.");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText("Selection explained")).toBeVisible();

    const request = fetchMock.mock.calls.find(([input]) => new URL(String(input)).pathname.endsWith("/chat/completions"));
    const body = JSON.parse(String(request?.[1]?.body));
    expect(body.context_attachments).toEqual([expect.objectContaining({
      source_kind: "project",
      source_label: "Project selection",
      text: "Selection review",
      truncated: false,
      sha256: expect.stringMatching(/^[a-f0-9]{64}$/),
    })]);
    expect(body.messages).toEqual([{ role: "user", content: "Explain this project title." }]);
  });

  it("opens selected text as an editable Notes draft without unmounting Workbench", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "project-1", name: "Notes review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/project");

    const heading = await screen.findByRole("heading", { name: "Notes review" });
    selectElementText(heading);
    await user.click(await screen.findByRole("button", { name: "Add note" }));

    expect(await screen.findByRole("tab", { name: "Project notes" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("textbox", { name: "Note title" })).toHaveValue("Note from Project selection");
    expect(screen.getByRole("textbox", { name: "Note body" })).toHaveValue("Notes review");
    expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname.endsWith("/observations") && init?.method === "POST")).toBe(false);
  });

  it("keeps chat working memory in the background", async () => {
    const entity = {
      created_at: "2026-07-12T10:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    };
    let chatPresent = true;
    let chatTitle = "Saved context";
    let chatRevision = 1;
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Memory review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/providers")) return new Response(JSON.stringify([{ ...entity, id: "provider-1", name: "Local analyst", provider_type: "vllm", endpoint: null, enabled: true, is_local: true, secret_ref: null, model_allowlist: ["model-1"], capabilities: { streaming: true }, privacy: { local_only: true, residency: [], permits_sensitive_data: false }, metadata: { default_model: "model-1" } }]), { status: 200 });
      if (path.endsWith("/chat-sessions/session-1") && init?.method === "DELETE") { chatPresent = false; return new Response(null, { status: 204 }); }
      if (path.endsWith("/chat-sessions/session-1") && init?.method === "PATCH") { const body = JSON.parse(String(init.body)); chatTitle = body.title; chatRevision += 1; return new Response(JSON.stringify({ ...entity, revision: chatRevision, id: "session-1", engagement_id: "engagement-1", title: chatTitle, provider_profile_id: "provider-1", model: "model-1", metadata: { message_count: 2 } }), { status: 200 }); }
      if (path.endsWith("/chat-sessions")) return new Response(JSON.stringify(chatPresent ? [{ ...entity, revision: chatRevision, id: "session-1", engagement_id: "engagement-1", title: chatTitle, provider_profile_id: "provider-1", model: "model-1", metadata: { message_count: 2 } }] : []), { status: 200 });
      if (path.endsWith("/chat/sessions/session-1/messages")) return new Response(JSON.stringify([
        { ...entity, id: "message-1", engagement_id: "engagement-1", session_id: "session-1", sequence: 1, role: "user", content: "Use port 8443", citations: [], usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 } },
        { ...entity, id: "message-2", engagement_id: "engagement-1", session_id: "session-1", sequence: 2, role: "assistant", content: "Port retained", citations: [], usage: { input_tokens: 2, output_tokens: 2, total_tokens: 4 } },
      ]), { status: 200 });
      if (path.endsWith("/chat/sessions/session-1/context")) return new Response(JSON.stringify({
        owner_type: "chat_session",
        owner_id: "session-1",
        status: "ready",
        context_window: 8192,
        max_output_tokens: 2048,
        target_input_tokens: 4608,
        estimated_input_tokens: 4700,
        compacted_through: 1,
        snapshot: {
          ...entity,
          id: "snapshot-1",
          owner_type: "chat_session",
          owner_id: "session-1",
          version: 1,
          status: "ready",
          compacted_through: 1,
          memory: { summary: "The selected service uses port 8443.", confirmed_facts: [], decisions: [], constraints: [], corrections: [], open_questions: [], evidence_ids: [], artifact_ids: [] },
          source_references: [{ source_kind: "chat_message", source_id: "message-1", sequence: 1 }],
          provider_profile_id: "provider-1",
          model: "model-1",
          prompt_version: "nebula-context-v1",
          usage: { input_tokens: 12, output_tokens: 4, total_tokens: 16 },
          cost_usd: 0,
        },
      }), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/sessions");

    await user.click(await screen.findByRole("tab", { name: /Analyst chat/ }));
    const conversationPanel = await screen.findByLabelText("Conversations");
    await user.click(screen.getByRole("button", { name: "Expand conversations panel" }));
    expect(conversationPanel.closest(".session-layout")).toHaveClass("conversation-panel-expanded");
    expect(localStorage.getItem("nebula.conversations.expanded")).toBe("true");
    await user.click((await screen.findByText("Saved context")).closest("button")!);
    expect(await screen.findByText("Port retained")).toBeVisible();
    expect(screen.queryByText("The selected service uses port 8443.")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Working memory" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => new URL(String(input)).pathname.endsWith("/chat/sessions/session-1/context"))).toBe(false);
    expect(screen.queryByRole("button", { name: "Save Assistant Response" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Rename conversation Saved context" }));
    const renameInput = screen.getByRole("textbox", { name: "Rename conversation Saved context" });
    await user.clear(renameInput);
    await user.type(renameInput, "Port review");
    await user.click(screen.getByRole("button", { name: "Save conversation name" }));
    expect(await screen.findByTitle("Port review")).toBeVisible();
    const renameCall = fetchMock.mock.calls.find(([input, request]) => new URL(String(input)).pathname.endsWith("/chat-sessions/session-1") && request?.method === "PATCH");
    expect(JSON.parse(String(renameCall?.[1]?.body))).toEqual({ title: "Port review", expected_revision: 1 });
    await user.click(screen.getByRole("button", { name: "Delete conversation Port review" }));
    const dialog = screen.getByRole("dialog", { name: "Delete Port review?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete conversation" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Delete conversation Port review" })).not.toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([input, request]) => new URL(String(input)).pathname.endsWith("/chat-sessions/session-1") && request?.method === "DELETE")).toBe(true);
  });

  it("starts Terminal automatically inside the reviewed container boundary", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const digest = "a".repeat(64);
    const incompleteDigest = "d".repeat(64);
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "ready", human_pty: "unavailable", container_terminal: "configured" }), { status: 200 });
      if (path.endsWith("/setup/status")) return new Response(JSON.stringify({
        core: { status: "ready", detail: null },
        scratch_project_id: "engagement-1",
        terminal: {
          status: "ready", runner_profile_id: "runner-1", candidates: [], detail: null,
          image_preparation: {
            phase: "ready", operation_id: "00000000-0000-4000-8000-000000000001", project_id: "engagement-1",
            progress_percent: 100, progress_indeterminate: false, can_cancel: false, can_retry: false,
            image_digest: `sha256:${"c".repeat(64)}`, detail: "Cached and verified",
          },
        },
        assistant: { status: "needs_model", provider_profile_id: null, detail: null },
      }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Terminal review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/container-terminal/capabilities")) return new Response(JSON.stringify({
        engagement_id: "engagement-1", ready: true, source_image: "docker.io/kalilinux/kali-rolling:latest", installed_packages: ["kali-linux-headless", "iputils-ping"], workspace: "/workspace",
        network: { mode: "unrestricted", runtime_network: "bridge", published_ports: [] },
        security: { container_user: "root", root_filesystem: "writable", linux_capabilities: [], no_new_privileges: true, host_network: false, runtime_socket: false, host_shell: false },
        limits: { cpu_count: 1, memory_mb: 512, pids: 128, timeout_seconds: 1800, output_bytes_per_stream: 2_000_000 },
        idle_timeout_seconds: 900, fresh_container: true, detail: null,
      }), { status: 200 });
      if (path.endsWith("/tool-packs")) return new Response(JSON.stringify([
        {
          ...entity, id: "pack-incomplete", publisher: "berylliumsec", name: "nebula-toolbox-staging", version: "0.1.0.dev6", manifest_digest: incompleteDigest,
          source: "local", trust_state: "developer", runtime_profile_id: "runner-1", image_locks: {}, status: "ready",
          tool_names: ["environment.shell_local"], permissions: ["workspace_write"],
        },
        {
          ...entity, id: "pack-1", publisher: "berylliumsec", name: "nebula-toolbox-staging", version: "0.1.0.dev6", manifest_digest: digest,
          source: "local", trust_state: "developer", runtime_profile_id: "runner-1", image_locks: {}, status: "ready",
          tool_names: [], permissions: ["network", "workspace_write"],
        },
      ]), { status: 200 });
      if (path.endsWith("/tools")) return new Response(JSON.stringify([
        {
          name: "environment.shell_local", pack_id: "pack-incomplete", pack_manifest_digest: incompleteDigest, description: "Unavailable local shell",
          risk_class: "workspace_write", requires_network: false, requires_approval: false, available: false,
        },
        {
          name: "environment.shell_local", pack_id: "pack-1", pack_manifest_digest: digest, description: "Local shell",
          risk_class: "workspace_write", requires_network: false, requires_approval: false, available: true,
        },
      ]), { status: 200 });
      if (path.endsWith("/tool-assignment")) {
        if (init?.method === "PUT") {
          return new Response(JSON.stringify({ ...entity, id: "assignment-1", engagement_id: "engagement-1", manifest_digest: digest, allowed_tool_names: ["environment.shell_local", "environment.shell_network"], enabled: true }), { status: 200 });
        }
        return new Response(JSON.stringify([]), { status: 200 });
      }
      if (path.endsWith("/container-terminal/preflight") && init?.method === "POST") return new Response(JSON.stringify({
        allowed: true,
        detail: "request is confined to the workspace",
        runtime: {
          source_image: "docker.io/kalilinux/kali-rolling:latest", interpreter: "/bin/bash", arguments: ["--noprofile", "--norc", "-i"],
          base_image: `docker.io/kalilinux/kali-rolling@sha256:${"b".repeat(64)}`, base_image_digest: `sha256:${"b".repeat(64)}`,
          image: `sha256:${"c".repeat(64)}`, image_digest: `sha256:${"c".repeat(64)}`, installed_packages: ["kali-linux-headless", "iputils-ping"],
          runner_profile_id: "runner-1", runner_profile_revision: 2, runner_runtime: "podman", runner_isolation: "rootless",
          runner_executable: "/usr/bin/podman", runner_platform: "linux/amd64",
        },
        network: { mode: "unrestricted", runtime_network: "bridge", published_ports: [] },
        security: { container_user: "root", root_filesystem: "writable", linux_capabilities: [], no_new_privileges: true, host_network: false, runtime_socket: false, host_shell: false },
        limits: { cpu_count: 1, memory_mb: 512, pids: 128, timeout_seconds: 1800, output_bytes_per_stream: 2_000_000 },
        workspace: "/workspace", policy_rule: "human_terminal_unrestricted",
        preview_fingerprint: "c".repeat(64), preview_token: "signed.preview", expires_at: "2026-07-13T18:00:00Z",
        idle_timeout_seconds: 900, fresh_container: true,
      }), { status: 200 });
      if (path.endsWith("/container-terminal/sessions") && init?.method === "POST") {
        return new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp();

    expect(await screen.findByRole("tab", { name: "Terminal" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("heading", { name: "Terminal" })).toBeVisible();
    expect(screen.getByText("Root + network")).toBeVisible();
    expect(screen.getAllByText("kali-linux-headless").length).toBeGreaterThan(0);
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, request]) => new URL(String(input)).pathname.endsWith("/container-terminal/sessions") && request?.method === "POST")).toBe(true));

    expect(fetchMock.mock.calls.some(([input, request]) => new URL(String(input)).pathname.endsWith("/tool-assignment") && request?.method === "PUT")).toBe(false);
    const preflightCall = fetchMock.mock.calls.find(([input, request]) => new URL(String(input)).pathname.endsWith("/container-terminal/preflight") && request?.method === "POST");
    expect(JSON.parse(String(preflightCall?.[1]?.body))).toEqual({ engagement_id: "engagement-1", columns: 100, rows: 30 });
    const startCall = fetchMock.mock.calls.find(([input, request]) => new URL(String(input)).pathname.endsWith("/container-terminal/sessions") && request?.method === "POST");
    expect(JSON.parse(String(startCall?.[1]?.body))).toMatchObject({ engagement_id: "engagement-1", preview_token: "signed.preview", preview_fingerprint: "c".repeat(64) });
    expect(startCall?.[1]?.signal?.aborted).toBe(false);
    await user.click(screen.getByRole("tab", { name: "Activity history" }));
    expect(screen.getByRole("heading", { name: "Terminal audit" })).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Analyst chat" }));
    await user.click(screen.getByRole("tab", { name: "Project notes" }));
    await user.click(screen.getByRole("tab", { name: "Terminal" }));
    expect(fetchMock.mock.calls.filter(([input, request]) => new URL(String(input)).pathname.endsWith("/container-terminal/sessions") && request?.method === "POST")).toHaveLength(1);
    expect(startCall?.[1]?.signal?.aborted).toBe(false);
  });

  it("creates and activates a new project from the switcher", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    let engagements = [{ ...entity, id: "engagement-old", name: "Old engagement", description: "", client_name: "Old client", status: "active", tags: [], metadata: {} }];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (url.pathname.endsWith("/engagements")) {
        if (init?.method === "POST") {
          const body = JSON.parse(String(init.body));
          const created = { ...entity, id: "engagement-new", name: body.name, description: body.description, client_name: body.client_name, status: body.status, tags: body.tags, metadata: {} };
          engagements = [...engagements, created];
          return new Response(JSON.stringify(created), { status: 201 });
        }
        return new Response(JSON.stringify(engagements), { status: 200 });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp();

    expect(await screen.findByTitle("Old engagement")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Switch project" }));
    await user.click(screen.getByRole("button", { name: "New project" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "New engagement");
    await user.type(screen.getByRole("textbox", { name: "Client name" }), "New client");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByTitle("New engagement")).toBeVisible();
    const createCall = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/api/v1/engagements") && init?.method === "POST");
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({ name: "New engagement", client_name: "New client", status: "draft" });
  });

  it("searches, creates, and inspects assets", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const engagement = { ...entity, id: "engagement-1", name: "Asset review", description: "", status: "active", tags: [], metadata: {} };
    let assets = [{ ...entity, id: "asset-old", engagement_id: "engagement-1", asset_type: "host", name: "old.example.test", address: "192.0.2.1", hostname: "old.example.test", criticality: "medium", exposed: true, tags: ["old"], metadata: {} }];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (url.pathname.endsWith("/engagements")) return new Response(JSON.stringify([engagement]), { status: 200 });
      if (url.pathname.endsWith("/assets")) {
        if (init?.method === "POST") {
          const body = JSON.parse(String(init.body));
          const created = { ...entity, id: "asset-new", ...body, exposed: body.exposed, metadata: {} };
          assets = [created, ...assets];
          return new Response(JSON.stringify(created), { status: 201 });
        }
        return new Response(JSON.stringify(assets), { status: 200 });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/assets");
    await screen.findByRole("heading", { name: "Assets" });

    const search = screen.getByRole("searchbox", { name: "Search assets" });
    await user.type(search, "missing");
    expect(screen.getByText("No assets match the current search and filters.")).toBeVisible();
    await user.clear(search);
    await user.keyboard("{Control>}n{/Control}");
    const dialog = screen.getByRole("dialog", { name: "Add asset" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "new.example.test");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Kind" }), "domain");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Criticality" }), "high");
    await user.click(within(dialog).getByRole("button", { name: "Add asset" }));

    expect(await screen.findByText("new.example.test")).toBeVisible();
    const row = screen.getByText("new.example.test").closest("tr");
    expect(row).not.toBeNull();
    await user.click(within(row!).getByRole("button", { name: "Inspect" }));
    expect(screen.getByRole("complementary", { name: "new.example.test" })).toBeVisible();
  });

  it("refreshes operator revisions after activation before editing the former active profile", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z" };
    let profiles: Array<typeof entity & { id: string; revision: number; display_name: string; email: string | null; role: string; active: boolean; activated_at: string | null; metadata: Record<string, unknown> }> = [
      { ...entity, id: "operator-alice", revision: 1, display_name: "Alice Analyst", email: "alice@example.test", role: "Lead", active: true, activated_at: entity.updated_at, metadata: {} },
      { ...entity, id: "operator-bob", revision: 1, display_name: "Bob Reviewer", email: null, role: "Reviewer", active: false, activated_at: null, metadata: {} },
    ];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (url.pathname.endsWith("/operator-profiles/operator-bob/activate")) {
        profiles = [
          { ...profiles[1], revision: 2, active: true, activated_at: "2026-07-12T12:00:00Z", updated_at: "2026-07-12T12:00:00Z" },
          { ...profiles[0], revision: 2, active: false, updated_at: "2026-07-12T12:00:00Z" },
        ];
        return new Response(JSON.stringify(profiles[0]), { status: 200 });
      }
      if (url.pathname.endsWith("/operator-profiles/operator-alice") && init?.method === "PATCH") {
        const body = JSON.parse(String(init.body));
        const updated = { ...profiles[1], revision: 3, display_name: body.display_name, email: body.email, role: body.role };
        profiles = [profiles[0], updated];
        return new Response(JSON.stringify(updated), { status: 200 });
      }
      if (url.pathname.endsWith("/operator-profiles")) return new Response(JSON.stringify(profiles), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/settings");
    await screen.findByRole("heading", { name: "Settings" });
    await user.click(screen.getByRole("link", { name: "Advanced settings" }));

    await user.click(await screen.findByRole("button", { name: "Activate" }));
    await user.click(await screen.findByRole("button", { name: "Edit Alice Analyst" }));
    const dialog = screen.getByRole("dialog", { name: "Edit operator" });
    const role = within(dialog).getByRole("textbox", { name: "Role" });
    await user.clear(role);
    await user.type(role, "Principal analyst");
    await user.click(within(dialog).getByRole("button", { name: "Save operator" }));

    const updateCall = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/api/v1/operator-profiles/operator-alice") && init?.method === "PATCH");
    expect(JSON.parse(String(updateCall?.[1]?.body))).toMatchObject({ role: "Principal analyst", expected_revision: 2 });
  });

  it("collects the required Vertex project and location when adding a provider", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/providers/provider-vertex/health")) return new Response(JSON.stringify({ provider_id: "provider-vertex", healthy: true, models: ["gemini-2.5-pro"], detail: null }), { status: 200 });
      if (url.pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (url.pathname.endsWith("/provider-catalog")) return new Response(JSON.stringify([
        { flavor: "bedrock", adapter: "bedrock", display_name: "AWS Bedrock", local: false, default_base_url: "https://bedrock-runtime.amazonaws.com", suggested_key_env: null, support_tier: "native", notes: "Uses the AWS credential chain." },
        { flavor: "vertex", adapter: "gemini", display_name: "Google Vertex AI", local: false, default_base_url: null, suggested_key_env: "GOOGLE_ACCESS_TOKEN", support_tier: "native", notes: "Requires project and location." },
      ]), { status: 200 });
      if (url.pathname.endsWith("/providers") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        return new Response(JSON.stringify({ ...entity, id: "provider-vertex", ...body, metadata: body.metadata }), { status: 201 });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/settings");
    await screen.findByRole("heading", { name: "Settings" });
    await user.click(screen.getByRole("link", { name: "Advanced settings" }));

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = screen.getByRole("dialog", { name: "Add model provider" });
    await user.selectOptions(within(dialog).getByLabelText("Provider type"), "vertex");
    await user.type(within(dialog).getByLabelText("Endpoint"), "https://us-central1-aiplatform.googleapis.com");
    await user.type(within(dialog).getByLabelText("Default model"), "gemini-2.5-pro");
    await user.type(within(dialog).getByLabelText("Context window (tokens)"), "16000");
    await user.type(within(dialog).getByLabelText("Maximum output tokens"), "1000");
    await user.type(within(dialog).getByLabelText("Google Cloud project"), "security-project");
    await user.type(within(dialog).getByLabelText("Vertex location"), "us-central1");
    await user.click(within(dialog).getByRole("button", { name: "Add provider" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname.endsWith("/providers") && init?.method === "POST")).toBe(true));
    const createCall = fetchMock.mock.calls.find(([input, init]) => new URL(String(input)).pathname.endsWith("/providers") && init?.method === "POST");
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({ provider_type: "vertex", metadata: { options: { project: "security-project", location: "us-central1", context_window: 16000, max_output_tokens: 1000 } } });
  });

  it("records a manual finding as an asset-linked, unverified candidate", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const asset = { ...entity, id: "asset-1", engagement_id: "engagement-1", asset_type: "domain", name: "portal.example.test", address: null, hostname: "portal.example.test", criticality: "high", exposed: true, tags: [], metadata: {} };
    let findings: Record<string, unknown>[] = [];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (url.pathname.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Manual review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (url.pathname.endsWith("/assets")) return new Response(JSON.stringify([asset]), { status: 200 });
      if (url.pathname.endsWith("/findings") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        const created = { ...entity, id: "finding-new", ...body, service_ids: [], evidence_ids: [], observation_ids: [], correlation_ids: [], remediation_id: null, verifier_id: null, verified_at: null };
        findings = [created];
        return new Response(JSON.stringify(created), { status: 201 });
      }
      if (url.pathname.endsWith("/findings")) return new Response(JSON.stringify(findings), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/findings");

    await screen.findByRole("heading", { name: "Findings" });
    const open = screen.getByRole("button", { name: "New finding" });
    await waitFor(() => expect(open).toBeEnabled());
    await user.click(open);
    const dialog = screen.getByRole("dialog", { name: "Create candidate finding" });
    expect(within(dialog).getByText(/unverified candidate only/i)).toBeVisible();
    await user.type(within(dialog).getByLabelText("Title"), "Reflected script injection");
    await user.type(within(dialog).getByLabelText("Description"), "Input is reflected without encoding.");
    await user.selectOptions(within(dialog).getByLabelText("Severity"), "high");
    await user.type(within(dialog).getByLabelText("Severity rationale"), "Internet reachable session theft risk");
    await user.click(within(dialog).getByLabelText("portal.example.test"));
    await user.type(within(dialog).getByLabelText("CVE identifiers"), "not-a-cve");
    await user.click(within(dialog).getByRole("button", { name: "Create candidate" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("CVE identifiers must look like CVE-2026-1234");
    expect(fetchMock.mock.calls.filter(([input, init]) => new URL(String(input)).pathname.endsWith("/findings") && init?.method === "POST")).toHaveLength(0);
    await user.clear(within(dialog).getByLabelText("CVE identifiers"));
    await user.type(within(dialog).getByLabelText("CVE identifiers"), "cve-2026-1234");
    await user.type(within(dialog).getByLabelText("CWE identifiers"), "cwe-79");
    await user.click(within(dialog).getByRole("button", { name: "Create candidate" }));

    expect(await screen.findByText("Reflected script injection")).toBeVisible();
    const createCall = fetchMock.mock.calls.find(([input, init]) => new URL(String(input)).pathname.endsWith("/findings") && init?.method === "POST");
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({ engagement_id: "engagement-1", status: "candidate", severity: "high", asset_ids: ["asset-1"], cve_ids: ["CVE-2026-1234"], cwe_ids: ["CWE-79"], metadata: { origin: "manual_operator_entry" } });
    expect(JSON.parse(String(createCall?.[1]?.body))).not.toHaveProperty("verifier_id");
  });

  it("edits, disables, and deletes provider profiles with current revisions", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z" };
    let provider: Record<string, unknown> | undefined = { ...entity, id: "provider-anthropic", revision: 3, name: "Anthropic review", provider_type: "anthropic", endpoint: "https://api.anthropic.com", enabled: true, is_local: false, secret_ref: "env:ANTHROPIC_API_KEY", model_allowlist: ["claude-old"], capabilities: { streaming: true }, privacy: { local_only: false, residency: [], permits_sensitive_data: false }, metadata: { default_model: "claude-old" } };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (url.pathname.endsWith("/provider-catalog")) return new Response(JSON.stringify([{ flavor: "anthropic", adapter: "anthropic", display_name: "Anthropic", local: false, default_base_url: "https://api.anthropic.com", suggested_key_env: "ANTHROPIC_API_KEY", support_tier: "native", notes: "Requires an explicit model." }]), { status: 200 });
      if (url.pathname.endsWith("/providers/provider-anthropic") && init?.method === "PATCH") {
        const request = JSON.parse(String(init.body));
        provider = { ...provider, ...request.changes, revision: Number(provider?.revision) + 1, updated_at: "2026-07-12T12:00:00Z" };
        return new Response(JSON.stringify(provider), { status: 200 });
      }
      if (url.pathname.endsWith("/providers/provider-anthropic") && init?.method === "DELETE") {
        provider = undefined;
        return new Response(null, { status: 204 });
      }
      if (url.pathname.endsWith("/providers")) return new Response(JSON.stringify(provider ? [provider] : []), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/settings");

    await screen.findByRole("heading", { name: "Settings" });
    await user.click(screen.getByRole("link", { name: "Advanced settings" }));
    await user.click(await screen.findByRole("button", { name: "Edit Anthropic review" }));
    const dialog = screen.getByRole("dialog", { name: "Edit Anthropic review" });
    const defaultModel = within(dialog).getByLabelText("Default model");
    await user.clear(defaultModel);
    expect(within(dialog).getByRole("button", { name: "Save provider" })).toBeDisabled();
    expect(within(dialog).getByText(/needs an explicit model ID/i)).toBeVisible();
    await user.type(defaultModel, "claude-new");
    await user.click(within(dialog).getByRole("button", { name: "Save provider" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Edit Anthropic review" })).not.toBeInTheDocument());

    let patchCalls = fetchMock.mock.calls.filter(([input, init]) => new URL(String(input)).pathname.endsWith("/providers/provider-anthropic") && init?.method === "PATCH");
    expect(JSON.parse(String(patchCalls[0][1]?.body))).toMatchObject({ changes: { model_allowlist: ["claude-new", "claude-old"], metadata: { default_model: "claude-new" } }, expected_revision: 3 });
    await user.click(screen.getByRole("button", { name: "Disable Anthropic review" }));
    await screen.findByRole("button", { name: "Enable Anthropic review" });
    patchCalls = fetchMock.mock.calls.filter(([input, init]) => new URL(String(input)).pathname.endsWith("/providers/provider-anthropic") && init?.method === "PATCH");
    expect(JSON.parse(String(patchCalls[1][1]?.body))).toEqual({ changes: { enabled: false }, expected_revision: 4 });
    const deleteProvider = screen.getByRole("button", { name: "Delete Anthropic review" });
    await user.click(deleteProvider);
    const confirmDelete = await screen.findByRole("button", { name: "Delete provider" });
    await waitFor(() => expect(confirmDelete).toHaveFocus());
    await user.keyboard("{Escape}");
    await waitFor(() => expect(deleteProvider).toHaveFocus());
    await user.click(deleteProvider);
    await user.click(await screen.findByRole("button", { name: "Delete provider" }));
    expect(await screen.findByText("No provider profiles")).toBeVisible();
    const deleteCall = fetchMock.mock.calls.find(([input, init]) => new URL(String(input)).pathname.endsWith("/providers/provider-anthropic") && init?.method === "DELETE");
    expect(new Headers(deleteCall?.[1]?.headers).get("If-Match")).toBe("5");
  });

  it("renders final reports as immutable while keeping export actions available", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 4 };
    const finding = { ...entity, id: "finding-1", engagement_id: "engagement-1", title: "Verified issue", description: "Evidence-backed issue", severity: "high", severity_rationale: "Material impact", status: "confirmed", asset_ids: [], evidence_ids: ["evidence-1"], cve_ids: [], cwe_ids: ["CWE-79"], verifier_id: "operator-reviewer", verified_at: entity.updated_at };
    const report = { ...entity, id: "report-final", engagement_id: "engagement-1", title: "Signed assessment", status: "final", executive_summary: "Approved final narrative", finding_ids: ["finding-1"], artifact_ids: ["artifact-report"], signed_off_by: "operator-reviewer", signed_off_at: entity.updated_at, metadata: {} };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (url.pathname.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Signed review", description: "", status: "complete", tags: [], metadata: {} }]), { status: 200 });
      if (url.pathname.endsWith("/findings")) return new Response(JSON.stringify([finding]), { status: 200 });
      if (url.pathname.endsWith("/reports")) return new Response(JSON.stringify([report]), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderApp("/reports");

    await screen.findByRole("heading", { name: "Reports" });
    expect(await screen.findByText(/immutable signed record/)).toBeVisible();
    expect(screen.getByLabelText("Status")).toBeDisabled();
    expect(screen.getByLabelText("Report title")).toHaveAttribute("readonly");
    expect(screen.getByLabelText("Executive summary")).toHaveAttribute("readonly");
    expect(screen.getByRole("checkbox", { name: /Verified issue/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Final report" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Export PDF" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Export engagement bundle" })).toBeEnabled();
    expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname.endsWith("/reports/report-final") && init?.method === "PATCH")).toBe(false);
  });

  it("resolves evidence attribution from operator profile IDs", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (url.pathname.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Evidence review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (url.pathname.endsWith("/operator-profiles")) return new Response(JSON.stringify([{ ...entity, id: "operator-alice", display_name: "Alice Analyst", email: null, role: "Lead", active: true, activated_at: entity.updated_at, metadata: {} }]), { status: 200 });
      if (url.pathname.endsWith("/evidence")) return new Response(JSON.stringify([{ ...entity, id: "evidence-1", engagement_id: "engagement-1", evidence_type: "operator_upload", title: "proof.txt", description: "Proof", artifact_id: "artifact-1", finding_id: null, asset_ids: [], sha256: "a".repeat(64), captured_at: entity.updated_at, captured_by: "operator-alice", source_version: null, metadata: { filename: "proof.txt", media_type: "text/plain", size: 5, source: "operator_upload" } }]), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderApp("/evidence");

    expect((await screen.findAllByText("Alice Analyst")).length).toBeGreaterThan(0);
    expect(screen.queryByText("operator-alice")).not.toBeInTheDocument();
  });

  it("updates a finding immediately when uploaded evidence links back to it", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const finding = { ...entity, id: "finding-1", engagement_id: "engagement-1", title: "Linked finding", description: "Needs proof", severity: "high", severity_rationale: "External exposure", status: "validated", asset_ids: [], evidence_ids: [], cve_ids: [], cwe_ids: [], verifier_id: null, verified_at: null };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (url.pathname.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Evidence review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (url.pathname.endsWith("/findings")) return new Response(JSON.stringify([finding]), { status: 200 });
      if (url.pathname.endsWith("/evidence/upload") && init?.method === "POST") return new Response(JSON.stringify({ ...entity, id: "evidence-new", engagement_id: "engagement-1", evidence_type: "operator_upload", title: "proof.txt", description: "", artifact_id: "artifact-new", finding_id: "finding-1", asset_ids: [], sha256: "b".repeat(64), captured_at: entity.updated_at, captured_by: null, source_version: null, metadata: { filename: "proof.txt", media_type: "text/plain", size: 5, source: "operator_upload" } }), { status: 201 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/evidence");

    await screen.findByRole("heading", { name: "Evidence" }, { timeout: 3_000 });
    await user.click(screen.getByRole("button", { name: "Add evidence" }));
    const dialog = screen.getByRole("dialog", { name: "Add evidence" });
    const file = new File(["proof"], "proof.txt", { type: "text/plain" });
    Object.defineProperty(file, "arrayBuffer", { value: async () => new TextEncoder().encode("proof").buffer });
    await user.upload(within(dialog).getByLabelText("File"), file);
    const title = within(dialog).getByLabelText("Title");
    await user.clear(title);
    await user.type(title, "proof.txt");
    await user.selectOptions(within(dialog).getByLabelText("Finding"), "finding-1");
    const store = within(dialog).getByRole("button", { name: "Store evidence" });
    expect(store).toBeEnabled();
    fireEvent.submit(dialog);
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input, init]) => new URL(String(input)).pathname.endsWith("/evidence/upload") && init?.method === "POST")).toHaveLength(1));
    expect(await screen.findByText("proof.txt")).toBeVisible();

    await user.click(screen.getByRole("link", { name: "Findings" }));
    const row = (await screen.findByText("Linked finding")).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row!).getAllByRole("cell")[4]).toHaveTextContent("1");
  });

  it("keeps Core online and explains analysis-only degradation when tooling endpoints are unavailable", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const featurePaths = ["/runner-profiles", "/scope", "/tool-assignment"];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Bounded review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/tool-catalog")) return new Response(JSON.stringify({ detail: "Tool platform is not configured" }), { status: 501 });
      if (featurePaths.some((suffix) => path.endsWith(suffix))) return new Response(JSON.stringify({ detail: "Feature not installed" }), { status: 404 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/settings");

    expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
    await user.click(screen.getByRole("link", { name: "Advanced settings" }));
    expect(await screen.findByText("Execution environments are not available in this Core build")).toBeVisible();
    expect(screen.getByText("Runner profiles are not available in this Core build")).toBeVisible();
    expect(screen.getByText("Scope editing is unavailable")).toBeVisible();
    expect(screen.getByText("Environment assignments are unavailable")).toBeVisible();
  });

  it("includes shell access by default without capability checkboxes", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const digest = "1".repeat(64);
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "ready", human_pty: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Default shell review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/tool-packs")) return new Response(JSON.stringify([{ ...entity, id: "pack-1", publisher: "berylliumsec", name: "nebula-toolbox", version: "0.1.0", manifest_digest: digest, source: "catalog", trust_state: "trusted", runtime_profile_id: "runner-1", image_locks: {}, status: "ready", tool_names: ["environment.shell_local", "environment.shell_network"], permissions: ["network", "workspace_write"], verified_at: entity.updated_at }]), { status: 200 });
      if (path.endsWith("/tools")) return new Response(JSON.stringify([
        { name: "environment.shell_local", pack_id: "pack-1", pack_manifest_digest: digest, description: "Local shell", risk_class: "workspace_write", requires_network: false, requires_approval: false, available: true },
        { name: "environment.shell_network", pack_id: "pack-1", pack_manifest_digest: digest, description: "Scoped network shell", risk_class: "active_scan", requires_network: true, requires_approval: false, available: true },
      ]), { status: 200 });
      if (path.endsWith("/tool-assignment")) {
        if (init?.method === "PUT") {
          const body = JSON.parse(String(init.body));
          return new Response(JSON.stringify({ ...entity, id: "assignment-1", engagement_id: "engagement-1", manifest_digest: digest, allowed_tool_names: body.tool_names, enabled: body.enabled }), { status: 200 });
        }
        return new Response(JSON.stringify([]), { status: 200 });
      }
      if (path.endsWith("/scope")) return new Response(JSON.stringify({ ...entity, id: "scope-1", engagement_id: "engagement-1", allowed_cidrs: [], allowed_domains: [], allowed_urls: [], allowed_ports: [], prohibited_actions: [], local_only: true, max_concurrency: 1, grants: [] }), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/settings");

    await user.click(await screen.findByRole("link", { name: "Advanced settings" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Assigned execution environment" })).toHaveValue(digest));
    expect(await screen.findByText(/shell access are included automatically/i)).toBeVisible();
    expect(screen.queryByRole("checkbox", { name: /environment\.shell_local/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /environment\.shell_network/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save assignment" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, request]) => new URL(String(input)).pathname.endsWith("/tool-assignment") && request?.method === "PUT")).toBe(true));
    const call = fetchMock.mock.calls.find(([input, request]) => new URL(String(input)).pathname.endsWith("/tool-assignment") && request?.method === "PUT");
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      manifest_digest: digest,
      tool_names: [],
      enabled: true,
    });
  });

  it("enables every assigned Toolbox capability automatically with bounded budgets", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "ready", human_pty: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Tool review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/providers")) return new Response(JSON.stringify([{ ...entity, id: "provider-1", name: "Structured provider", provider_type: "vllm", enabled: true, is_local: true, model_allowlist: ["model-1"], capabilities: { streaming: true, tool_calling: true, strict_structured_output: true }, capability_verifications: { "model-1": { model: "model-1", status: "verified", checked_at: "2026-07-12T10:00:00Z", contract_version: "required-tool-v1" } }, privacy: { local_only: true, residency: [] }, metadata: { default_model: "model-1" } }]), { status: 200 });
      if (path.endsWith("/tool-assignment")) return new Response(JSON.stringify([{ id: "assignment-1", engagement_id: "engagement-1", manifest_digest: "sha256:toolbox", tool_names: ["environment.run_network"], enabled: true, revision: 1 }]), { status: 200 });
      if (path.endsWith("/tool-packs")) return new Response(JSON.stringify([{ id: "toolbox", publisher: "berylliumsec", name: "nebula-toolbox", version: "0.1.0", manifest_digest: "sha256:toolbox", source: "catalog", trust_state: "trusted", runtime_profile_id: "runner-1", image_locks: {}, status: "ready", tool_names: ["environment.run_network"], permissions: ["network"] }]), { status: 200 });
      if (path.endsWith("/tools")) return new Response(JSON.stringify([{ name: "environment.run_network", pack_id: "toolbox", pack_manifest_digest: "sha256:toolbox", description: "Run an indexed network command", risk_class: "active_scan", requires_network: true, requires_approval: false, available: true }]), { status: 200 });
      if (path.endsWith("/missions") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        return new Response(JSON.stringify({ ...entity, id: "run-1", engagement_id: "engagement-1", objective: body.objective, status: "queued", metadata: {} }), { status: 202 });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/project");

    await screen.findByRole("heading", { name: "Tool review" });
    await user.click(screen.getByRole("button", { name: "Automate task" }));
    let dialog = screen.getByRole("dialog", { name: "Automate task" });
    expect(dialog.parentElement?.parentElement).toBe(document.body);
    expect(within(dialog).getByText("Advanced")).toBeVisible();
    expect(within(dialog).getByLabelText("Provider")).not.toBeVisible();
    await user.click(within(dialog).getByText("Advanced"));
    expect(within(dialog).getByText(/core enforces project scope/i)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Automate task" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Enter a mission objective.");
    expect(fetchMock.mock.calls.some(([input, request]) => new URL(String(input)).pathname.endsWith("/missions") && request?.method === "POST")).toBe(false);
    await user.type(within(dialog).getByRole("textbox", { name: "Objective" }), "Scan the assigned target");
    expect(within(dialog).getByText("environment.run_network")).toBeVisible();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("spinbutton", { name: "Maximum tool calls" })).toHaveValue(50);
    expect(within(dialog).getByRole("spinbutton", { name: "Maximum concurrency" })).toHaveValue(2);
    await user.click(within(dialog).getByRole("button", { name: "Close automation dialog" }));
    await user.click(screen.getByRole("button", { name: "Automate task" }));
    dialog = screen.getByRole("dialog", { name: "Automate task" });
    await user.click(within(dialog).getByText("Advanced"));
    expect(within(dialog).getByText("environment.run_network")).toBeVisible();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Automate task" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, request]) => new URL(String(input)).pathname.endsWith("/missions") && request?.method === "POST")).toBe(true));
    const call = fetchMock.mock.calls.find(([input, request]) => new URL(String(input)).pathname.endsWith("/missions") && request?.method === "POST");
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ tool_names: ["environment.run_network"], max_tool_calls: 50, max_concurrency: 2 });
  });

  it("prepares and assigns the signed official Toolbox on first automation use", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const digest = `sha256:${"8".repeat(64)}`;
    let installed = false;
    let assignment: Record<string, unknown> | undefined;
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "ready", human_pty: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "First automation", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/providers")) return new Response(JSON.stringify([{ ...entity, id: "provider-1", name: "Structured provider", provider_type: "vllm", enabled: true, is_local: true, model_allowlist: ["model-1"], capabilities: { streaming: true, tool_calling: true }, capability_verifications: { "model-1": { model: "model-1", status: "verified", checked_at: entity.updated_at, contract_version: "required-tool-v1" } }, privacy: { local_only: true, residency: [] }, metadata: { default_model: "model-1" } }]), { status: 200 });
      if (path.endsWith("/tool-catalog")) return new Response(JSON.stringify([{ id: "berylliumsec/nebula-toolbox@0.1.0", publisher: "berylliumsec", name: "nebula-toolbox", version: "0.1.0", description: "Official Toolbox", manifest_digest: digest, licenses: ["Apache-2.0"], platforms: ["linux/amd64"], tool_names: ["environment.run_network"], permissions: ["network"], signed: true, collection_id: "nebula-toolbox", collection_name: "Nebula Toolbox", collection_order: 0 }]), { status: 200 });
      if (path.endsWith("/runner-profiles")) return new Response(JSON.stringify([{ ...entity, id: "runner-1", name: "Local Podman", runtime: "podman", executable: "/usr/bin/podman", platform: "linux/amd64", isolation: "rootless", enabled: true, healthy: true }]), { status: 200 });
      if (path.endsWith("/tool-collections/install") && init?.method === "POST") {
        installed = true;
        return new Response(JSON.stringify([{ ...entity, id: "pack-1", catalog_id: "berylliumsec/nebula-toolbox@0.1.0", publisher: "berylliumsec", name: "nebula-toolbox", version: "0.1.0", manifest_digest: digest, source: "catalog", trust_state: "trusted", runtime_profile_id: "runner-1", image_locks: {}, status: "ready", tool_names: ["environment.run_network"], permissions: ["network"] }]), { status: 201 });
      }
      if (path.endsWith("/tool-packs")) return new Response(JSON.stringify(installed ? [{ ...entity, id: "pack-1", publisher: "berylliumsec", name: "nebula-toolbox", version: "0.1.0", manifest_digest: digest, source: "catalog", trust_state: "trusted", runtime_profile_id: "runner-1", image_locks: {}, status: "ready", tool_names: ["environment.run_network"], permissions: ["network"] }] : []), { status: 200 });
      if (path.endsWith("/tools")) return new Response(JSON.stringify(installed ? [{ name: "environment.run_network", pack_id: "pack-1", pack_manifest_digest: digest, description: "Run a scoped network command", risk_class: "active_scan", requires_network: true, requires_approval: false, available: true }] : []), { status: 200 });
      if (path.endsWith("/tool-assignment")) {
        if (init?.method === "PUT") {
          const body = JSON.parse(String(init.body));
          assignment = body;
          return new Response(JSON.stringify({ ...entity, id: "assignment-1", engagement_id: "engagement-1", manifest_digest: digest, allowed_tool_names: body.tool_names, enabled: true }), { status: 200 });
        }
        return new Response(JSON.stringify(assignment ? [{ ...entity, id: "assignment-1", engagement_id: "engagement-1", manifest_digest: digest, allowed_tool_names: assignment.tool_names, enabled: true }] : []), { status: 200 });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/project");

    await screen.findByRole("heading", { name: "First automation" });
    await user.click(screen.getByRole("button", { name: "Automate task" }));
    const dialog = screen.getByRole("dialog", { name: "Automate task" });
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, request]) => new URL(String(input)).pathname.endsWith("/tool-collections/install") && request?.method === "POST")).toBe(true));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, request]) => new URL(String(input)).pathname.endsWith("/tool-assignment") && request?.method === "PUT")).toBe(true));
    await user.click(within(dialog).getByText("Advanced"));
    expect(within(dialog).getByText("environment.run_network")).toBeVisible();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(assignment).toMatchObject({ manifest_digest: digest, tool_names: ["environment.run_network"], enabled: true });
  });

  it("installs the signed Nebula Toolbox environment with one action", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const catalog = [{
      id: "berylliumsec/nebula-toolbox@0.1.0", publisher: "berylliumsec", name: "nebula-toolbox", version: "0.1.0", description: "Nebula Toolbox",
      manifest_digest: "1".repeat(64), licenses: [], platforms: ["linux/amd64", "linux/arm64"],
      tool_names: ["environment.search", "environment.help", "environment.run_local", "environment.run_network", "environment.run_invasive", "environment.shell_local", "environment.shell_network"], permissions: ["network", "workspace_workspace_write"], signed: true,
      collection_id: "nebula-toolbox", collection_name: "Nebula Toolbox", collection_order: 0,
    }];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "ready", human_pty: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Collection review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/runner-profiles")) return new Response(JSON.stringify([{ ...entity, id: "runner-1", name: "Podman", runtime: "podman", executable: "/usr/bin/podman", platform: "linux/amd64", isolation: "rootless", healthy: true, state: "ready" }]), { status: 200 });
      if (path.endsWith("/tool-catalog")) return new Response(JSON.stringify(catalog), { status: 200 });
      if (path.endsWith("/tool-collections/install") && init?.method === "POST") return new Response(JSON.stringify([]), { status: 201 });
      if (path.endsWith("/scope")) return new Response(JSON.stringify({ ...entity, id: "scope:engagement-1", engagement_id: "engagement-1", allowed_cidrs: [], allowed_domains: [], allowed_urls: [], allowed_ports: [], prohibited_actions: [], local_only: true, max_concurrency: 1, grants: [] }), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/settings");

    await user.click(await screen.findByRole("link", { name: "Advanced settings" }));
    const button = await screen.findByRole("button", { name: "Install Nebula Toolbox" });
    await user.click(button);
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, request]) => new URL(String(input)).pathname.endsWith("/tool-collections/install") && request?.method === "POST")).toBe(true));
    const call = fetchMock.mock.calls.find(([input, request]) => new URL(String(input)).pathname.endsWith("/tool-collections/install") && request?.method === "POST");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ collection_id: "nebula-toolbox", runtime_profile_id: "runner-1" });
  });

  it("removes disabled environments completely from active UI and permits reinstall", async () => {
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const digest = "1".repeat(64);
    const catalog = [{
      id: "berylliumsec/nebula-toolbox@0.1.0", publisher: "berylliumsec", name: "nebula-toolbox", version: "0.1.0", description: "Nebula Toolbox",
      manifest_digest: digest, licenses: [], platforms: ["linux/amd64", "linux/arm64"], tool_names: ["environment.shell_local"], permissions: ["workspace_write"], signed: true,
      collection_id: "nebula-toolbox", collection_name: "Nebula Toolbox", collection_order: 0,
    }];
    let status: "ready" | "disabled" = "ready";
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "ready", human_pty: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Removal review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/runner-profiles")) return new Response(JSON.stringify([{ ...entity, id: "runner-1", name: "Podman", runtime: "podman", executable: "/usr/bin/podman", platform: "linux/amd64", isolation: "rootless", healthy: true, state: "ready" }]), { status: 200 });
      if (path.endsWith("/tool-catalog")) return new Response(JSON.stringify(catalog), { status: 200 });
      if (path.endsWith("/tool-packs/pack-1") && init?.method === "DELETE") {
        status = "disabled";
        return new Response(null, { status: 204 });
      }
      if (path.endsWith("/tool-packs")) return new Response(JSON.stringify([{
        id: "pack-1", publisher: "berylliumsec", name: "nebula-toolbox", version: "0.1.0", manifest_digest: digest,
        source: "catalog", trust_state: "trusted", runtime_profile_id: "runner-1", image_locks: {}, status,
        tool_names: ["environment.shell_local"], permissions: ["workspace_write"], verified_at: entity.updated_at,
      }]), { status: 200 });
      if (path.endsWith("/tool-assignment")) return new Response(JSON.stringify([{
        id: "assignment-1", engagement_id: "engagement-1", manifest_digest: digest,
        tool_names: ["environment.shell_local"], enabled: true, revision: 1,
      }]), { status: 200 });
      if (path.endsWith("/tools")) return new Response(JSON.stringify([{
        name: "environment.shell_local", pack_id: "pack-1", pack_manifest_digest: digest,
        description: "Run local code", risk_class: "workspace_write", requires_network: false,
        requires_approval: false, available: status === "ready", unavailable_reason: status === "disabled" ? "pack is disabled" : null,
      }]), { status: 200 });
      if (path.endsWith("/scope")) return new Response(JSON.stringify({ ...entity, id: "scope:engagement-1", engagement_id: "engagement-1", allowed_cidrs: [], allowed_domains: [], allowed_urls: [], allowed_ports: [], prohibited_actions: [], local_only: true, max_concurrency: 1, grants: [] }), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/settings");

    await user.click(await screen.findByRole("link", { name: "Advanced settings" }));
    const remove = await screen.findByRole("button", { name: "Remove nebula-toolbox" });
    expect(screen.getAllByRole("button", { name: "Installed" }).length).toBeGreaterThan(0);
    await user.click(remove);
    const dialog = screen.getByRole("dialog", { name: "Remove nebula-toolbox?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove environment" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, request]) => new URL(String(input)).pathname.endsWith("/tool-packs/pack-1") && request?.method === "DELETE")).toBe(true));
    expect(await screen.findByText("No execution environment installed")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Remove nebula-toolbox" })).not.toBeInTheDocument();
    expect(screen.queryByText("disabled")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install Nebula Toolbox" })).toBeEnabled();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Assigned execution environment" })).toHaveValue(""));
    expect(screen.queryByText("environment.shell_local")).not.toBeInTheDocument();
  });

  it("shows replayed tool-pack progress in Settings", async () => {
    class ToolPackWebSocket extends EventTarget {
      static instance?: ToolPackWebSocket;
      readonly url: string;
      readonly protocols: string[];
      constructor(url: string | URL, protocols: string | string[]) {
        super();
        this.url = String(url);
        this.protocols = typeof protocols === "string" ? [protocols] : protocols;
        ToolPackWebSocket.instance = this;
      }
      close() { this.dispatchEvent(new CloseEvent("close", { code: 1000 })); }
    }
    const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1 };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (path.endsWith("/engagements")) return new Response(JSON.stringify([{ ...entity, id: "engagement-1", name: "Progress review", description: "", status: "active", tags: [], metadata: {} }]), { status: 200 });
      if (path.endsWith("/scope")) return new Response(JSON.stringify({ ...entity, id: "scope:engagement-1", engagement_id: "engagement-1", allowed_cidrs: [], allowed_domains: [], allowed_urls: [], allowed_ports: [], prohibited_actions: [], local_only: true, max_concurrency: 1, grants: [] }), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", ToolPackWebSocket);
    const user = userEvent.setup();
    renderApp("/settings");

    await user.click(await screen.findByRole("link", { name: "Advanced settings" }));
    const progressRegion = await screen.findByRole("region", { name: "Tool-pack installation progress" });
    await waitFor(() => expect(ToolPackWebSocket.instance).toBeDefined());
    expect(ToolPackWebSocket.instance?.url).toContain("/api/v1/tool-packs/events/ws?after_sequence=0");
    expect(ToolPackWebSocket.instance?.protocols[0]).toBe("nebula.tool-packs.v1");
    act(() => ToolPackWebSocket.instance?.dispatchEvent(new Event("open")));
    act(() => ToolPackWebSocket.instance?.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ kind: "event", event: { sequence: 1, occurred_at: "2026-07-12T19:00:00Z", operation_id: "operation-1", operation: "install_catalog", phase: "verifying", pack_identity: "berylliumsec/network@1.0.0", manifest_digest: "a".repeat(64) } }) })));
    expect(await within(progressRegion).findByText("berylliumsec/network@1.0.0")).toBeVisible();
    expect(within(progressRegion).getByText("verifying")).toBeVisible();
    act(() => ToolPackWebSocket.instance?.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ kind: "event", event: { sequence: 2, occurred_at: "2026-07-12T19:00:01Z", operation_id: "operation-1", operation: "install_catalog", phase: "ready", pack_identity: "berylliumsec/network@1.0.0", manifest_digest: "a".repeat(64), result_status: "ready" } }) })));
    expect(await within(progressRegion).findByText("ready")).toBeVisible();
    expect(within(progressRegion).queryByText("verifying")).not.toBeInTheDocument();
  });
});
