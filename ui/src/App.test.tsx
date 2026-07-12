import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { ThemeProvider } from "./state/ThemeContext";
import { WorkspaceProvider } from "./state/WorkspaceContext";

function renderApp(route = "/") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <ThemeProvider>
        <WorkspaceProvider>
          <App />
        </WorkspaceProvider>
      </ThemeProvider>
    </MemoryRouter>,
  );
}

describe("Nebula workspace", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Core offline")));
  });

  afterEach(() => vi.unstubAllGlobals());

  it("exposes every primary workspace destination", async () => {
    renderApp();
    for (const label of ["Overview", "Sessions", "Agents", "Assets", "Findings", "Evidence", "Knowledge", "Reports", "Settings"]) {
      expect(screen.getByRole("link", { name: label })).toBeVisible();
    }
    expect(await screen.findByRole("heading", { name: "Good afternoon, Jordan" })).toBeVisible();
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
    await user.click(screen.getByRole("button", { name: /High contrast/ }));
    expect(document.documentElement).toHaveAttribute("data-theme", "high-contrast");
    expect(localStorage.getItem("nebula.theme")).toBe("high-contrast");
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
      if (url.pathname.endsWith("/approvals")) {
        return new Response(JSON.stringify([{ ...entity, id: "approval-first", engagement_id: "engagement-first", run_id: "run-first", status: "pending", risk_class: "active_scan", exact_request: { tool_name: "scan.tcp", arguments: { ports: [443] } }, policy_rationale: "Active scan approval", requested_by: "network-specialist", requested_at: entity.created_at, expected_effects: ["Probe the target"] }]), { status: 200 });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", OnlineWebSocket);

    renderApp();

    expect(await screen.findByRole("heading", { name: "Live engagement" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Show activity center" })).toBeVisible();
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
          model_allowlist: ["model-1"],
          capabilities: { streaming: true },
          privacy: { local_only: false, residency: [], permits_sensitive_data: true },
          metadata: { default_model: "model-1" },
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
    const confirm = vi.fn(() => true);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", confirm);
    const user = userEvent.setup();

    renderApp("/sessions");
    await user.click(await screen.findByRole("tab", { name: /Analyst chat/ }));
    expect(await screen.findByRole("combobox", { name: "Chat provider" })).toHaveValue("provider-1");
    expect(screen.getByRole("combobox", { name: "Chat model" })).toHaveValue("model-1");
    await user.type(screen.getByRole("textbox", { name: "Message the analyst assistant" }), "Review the scope");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    expect(await screen.findByText("Bounded answer")).toBeVisible();
    expect(confirm).toHaveBeenCalledOnce();
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

  it("creates and activates a new engagement from the switcher", async () => {
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

    expect(await screen.findByRole("heading", { name: "Old engagement" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Switch engagement" }));
    await user.click(screen.getByRole("button", { name: "New engagement" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "New engagement");
    await user.type(screen.getByRole("textbox", { name: "Client name" }), "New client");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("heading", { name: "New engagement" })).toBeVisible();
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
    await user.click(screen.getByRole("button", { name: "Add asset" }));
    const dialog = screen.getByRole("dialog", { name: "Add asset" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "new.example.test");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Kind" }), "domain");
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Criticality" }), "high");
    await user.click(within(dialog).getByRole("button", { name: "Add asset" }));

    expect(await screen.findByText("new.example.test")).toBeVisible();
    const row = screen.getByText("new.example.test").closest("tr");
    expect(row).not.toBeNull();
    await user.click(within(row!).getByRole("button", { name: "Inspect" }));
    expect(screen.getByRole("dialog", { name: "new.example.test" })).toBeVisible();
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

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = screen.getByRole("dialog", { name: "Add model provider" });
    await user.selectOptions(within(dialog).getByLabelText("Provider type"), "vertex");
    await user.type(within(dialog).getByLabelText("Endpoint"), "https://us-central1-aiplatform.googleapis.com");
    await user.type(within(dialog).getByLabelText("Default model"), "gemini-2.5-pro");
    await user.type(within(dialog).getByLabelText("Google Cloud project"), "security-project");
    await user.type(within(dialog).getByLabelText("Vertex location"), "us-central1");
    await user.click(within(dialog).getByRole("button", { name: "Add provider" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname.endsWith("/providers") && init?.method === "POST")).toBe(true));
    const createCall = fetchMock.mock.calls.find(([input, init]) => new URL(String(input)).pathname.endsWith("/providers") && init?.method === "POST");
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({ provider_type: "vertex", metadata: { options: { project: "security-project", location: "us-central1" } } });
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
    vi.stubGlobal("confirm", vi.fn(() => true));
    const user = userEvent.setup();
    renderApp("/settings");

    await screen.findByRole("heading", { name: "Settings" });
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
    await user.click(screen.getByRole("button", { name: "Delete Anthropic review" }));
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
    expect(screen.getByRole("button", { name: "Markdown" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "JSON" })).toBeEnabled();
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

    expect(await screen.findByText("Alice Analyst")).toBeVisible();
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
});
