import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, chatRequestBody } from "./client";
import type { ChatStreamEvent, ProviderHealth } from "./types";

const diagnostics = vi.hoisted(() => ({ logDiagnostic: vi.fn() }));
vi.mock("../diagnostics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../diagnostics")>()),
  logDiagnostic: diagnostics.logDiagnostic,
}));

describe("ApiClient", () => {
  it("sends the last validator on polled reads and treats 304 as unchanged", async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ revision: 3 }), { status: 200, headers: { ETag: '"state-3"' } }))
      .mockResolvedValueOnce(new Response(null, { status: 304, headers: { ETag: '"state-3"' } }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    diagnostics.logDiagnostic.mockClear();

    const first = await client.requestIfChanged<{ revision: number }>("chat/sessions/s/state", undefined);
    expect(first).toEqual({ value: { revision: 3 }, etag: '"state-3"' });
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).has("If-None-Match")).toBe(false);

    await expect(client.requestIfChanged("chat/sessions/s/state", first?.etag)).resolves.toBeUndefined();
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get("If-None-Match")).toBe('"state-3"');
    // An unchanged answer is not a failed request.
    expect(diagnostics.logDiagnostic).not.toHaveBeenCalled();
  });

  it("asks for the compact pending-turn view while waiting on a turn", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "turn-1", session_id: "s", status: "waiting_callback", revision: 7,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    await expect(client.getPendingChatTurnStatus("s")).resolves.toEqual({ id: "turn-1", status: "waiting_callback", revision: 7 });
    expect(String(fetchMock.mock.calls[0][0])).toBe("http://127.0.0.1:8765/api/v1/chat/sessions/s/pending-turn?view=status");
  });

  it("sends MCP imports as snake_case and maps the preview report", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      dry_run: true, created: 1, updated: 1, unchanged: 0, replaced: 0, skipped: 0, invalid: 0,
      entries: [{
        source_name: "remote", name: "remote", action: "create", transport: "streamable_http",
        command: null, arguments: [], url: "https://mcp.example.test/mcp", profile_id: null,
        secrets: [{ target: "Authorization bearer token", source: "environment", reference: "env:TOKEN" }],
        enabled: true, default_approval: "ask", needs_trust: false, needs_probe: true,
        warnings: [], error: null,
      }, {
        source_name: "local", name: "local", action: "update", transport: "stdio",
        command: "/usr/bin/npx", arguments: ["local-mcp@2"], profile_id: "p1",
        changes: [{ field: "args", before: "local-mcp", after: "local-mcp@2" }, { field: "env TOKEN", before: "${TOKEN}", after: null }],
        enabled: false, default_approval: "risk_based", needs_trust: true, needs_probe: false,
      }],
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    const config = { mcpServers: { remote: { url: "https://mcp.example.test/mcp" } } };

    await expect(client.importMcpServers({
      config, dryRun: true, onConflict: "update", sourceName: "mcp.json",
      defaults: { enabled: true, defaultApproval: "ask" }, trustLocalPrograms: true,
    })).resolves.toEqual({
      dryRun: true, created: 1, updated: 1, unchanged: 0, replaced: 0, skipped: 0, invalid: 0,
      entries: [{
        sourceName: "remote", name: "remote", action: "create", transport: "streamable_http",
        command: undefined, arguments: [], url: "https://mcp.example.test/mcp", profileId: undefined,
        secrets: [{ target: "Authorization bearer token", source: "environment", reference: "env:TOKEN" }],
        changes: [], enabled: true, defaultApproval: "ask", needsTrust: false, needsProbe: true,
        warnings: [], error: undefined,
      }, {
        sourceName: "local", name: "local", action: "update", transport: "stdio",
        command: "/usr/bin/npx", arguments: ["local-mcp@2"], url: undefined, profileId: "p1", secrets: [],
        changes: [{ field: "args", before: "local-mcp", after: "local-mcp@2" }, { field: "env TOKEN", before: "${TOKEN}", after: undefined }],
        enabled: false, defaultApproval: "risk_based", needsTrust: true, needsProbe: false,
        warnings: [], error: undefined,
      }],
    });
    expect(String(fetchMock.mock.calls[0][0])).toBe("http://127.0.0.1:8765/api/v1/mcp-servers/import");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      config, dry_run: true, on_conflict: "update", source_name: "mcp.json",
      defaults: { enabled: true, default_approval: "ask" }, trust_local_programs: true,
    });
  });

  it("loads OpenRouter's upstream provider directory with locations", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify([
      { slug: "gmicloud", name: "GMICloud", headquarters: "US", datacenters: ["US"] },
      { slug: "baidu", name: "Baidu", headquarters: null, datacenters: [] },
      { slug: 4, name: "Broken" },
    ]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.listOpenRouterUpstreamProviders()).resolves.toEqual([
      { slug: "gmicloud", name: "GMICloud", headquarters: "US", datacenters: ["US"] },
      { slug: "baidu", name: "Baidu" },
    ]);
    expect(String(fetchMock.mock.calls[0][0])).toBe("http://127.0.0.1:8765/api/v1/providers/openrouter/upstream-providers");
  });

  it("maps authoritative goal elapsed, child, and completion evidence", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "goal-1", engagement_id: "project", session_id: "session",
      objective: "Verify", completion_criteria: ["Checks pass"], plan: [],
      current_step: 1, status: "completed", elapsed_seconds: 12.5,
      children_started: 1, child_budget: 2,
      usage: { input_tokens: 3, output_tokens: 2, total_tokens: 5 },
      linked_turn_ids: ["turn-1"], completion_summary: "Done",
      completion_evidence: [{ kind: "test", result: "passed" }],
      skill_snapshots: [{
        name: "review", path: "/workspace/.agents/skills/review/SKILL.md",
        source: "project", root: "/workspace/.agents/skills", sha256: "a".repeat(64),
        instructions: "not mapped into UI state",
      }], revision: 4,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.getChatGoal("session")).resolves.toMatchObject({
      elapsedSeconds: 12.5,
      childrenStarted: 1,
      childBudget: 2,
      completionSummary: "Done",
      completionEvidence: [{ kind: "test", result: "passed" }],
      skillSnapshots: [{
        name: "review", path: "/workspace/.agents/skills/review/SKILL.md",
        source: "project", root: "/workspace/.agents/skills", sha256: "a".repeat(64),
      }],
    });
  });
  it("replaces goal skills with exact identities and an optimistic revision", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "goal-1", engagement_id: "project", session_id: "session",
      objective: "Verify", completion_criteria: ["Checks pass"], plan: [],
      current_step: 0, status: "running", usage: {}, linked_turn_ids: [],
      completion_evidence: [], skill_snapshots: [], revision: 5,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await client.replaceChatGoalSkills("session/one", {
      expectedRevision: 4,
      skills: [{ name: "review", path: "/workspace/.agents/skills/review/SKILL.md" }],
    });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/chat/sessions/session%2Fone/goal/skills",
    );
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "PUT",
      body: JSON.stringify({
        expected_revision: 4,
        skills: [{ name: "review", path: "/workspace/.agents/skills/review/SKILL.md" }],
      }),
    });
  });
  it("serializes a Core-owned goal identity on provider turns", () => {
    expect(chatRequestBody({
      providerId: "provider",
      sessionId: "session",
      goalId: "goal",
      messages: [{ role: "user", content: "Continue" }],
    }, true)).toMatchObject({ session_id: "session", goal_id: "goal", stream: true });
  });
  it("serializes an exact native skill identity on provider turns", () => {
    expect(chatRequestBody({
      providerId: "provider",
      skill: { name: "review", path: "/workspace/.agents/skills/review/SKILL.md" },
      messages: [{ role: "user", content: "$review Continue" }],
    }, true)).toMatchObject({
      skill: { name: "review", path: "/workspace/.agents/skills/review/SKILL.md" },
      stream: true,
    });
  });

  it("serializes a revision-bound runtime switch confirmation", () => {
    expect(chatRequestBody({
      providerId: "provider",
      model: "smaller-model",
      runtimeSwitchConfirmation: "a".repeat(64),
      messages: [{ role: "user", content: "Continue" }],
    }, true)).toMatchObject({
      model: "smaller-model",
      runtime_switch_confirmation: "a".repeat(64),
      stream: true,
    });
  });

  it("maps runtime switch preflight capacity and confirmation", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      session_id: "session-1",
      session_revision: 4,
      current_provider_id: "provider-1",
      current_model: "large-model",
      target_provider_id: "provider-1",
      target_model: "small-model",
      compatible: true,
      requires_compaction_confirmation: true,
      confirmation_token: "b".repeat(64),
      estimated_active_input_tokens: 12_000,
      target_context_window: 8_000,
      target_input_tokens: 4_500,
      target_max_output_tokens: 2_000,
      metadata_revision: "catalog-2",
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.preflightChatRuntimeSwitch("session-1", {
      providerId: "provider-1",
      model: "small-model",
      toolsEnabled: true,
      expectedSessionRevision: 4,
    })).resolves.toMatchObject({
      sessionId: "session-1",
      compatible: true,
      requiresCompactionConfirmation: true,
      confirmationToken: "b".repeat(64),
      targetInputTokens: 4_500,
      metadataRevision: "catalog-2",
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/chat/sessions/session-1/runtime-switch/preflight",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      provider_id: "provider-1",
      model: "small-model",
      tools_enabled: true,
      expected_session_revision: 4,
    });
  });

  it("saves a reviewed runtime switch on the conversation", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z",
      revision: 5, id: "session-1", engagement_id: "engagement-1", title: "Scope review",
      provider_profile_id: "provider-1", model: "small-model", metadata: {},
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.applyChatRuntimeSwitch("session-1", {
      providerId: "provider-1",
      model: "small-model",
      toolsEnabled: true,
      expectedSessionRevision: 4,
      confirmationToken: "b".repeat(64),
    })).resolves.toMatchObject({ id: "session-1", model: "small-model", revision: 5 });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/chat/sessions/session-1/runtime-switch",
    );
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      provider_id: "provider-1",
      model: "small-model",
      tools_enabled: true,
      expected_session_revision: 4,
      confirmation_token: "b".repeat(64),
    });
  });

  it("loads native skills from the engagement-scoped Nebula catalog", async () => {
    const skills = [{
      name: "review",
      path: "/workspace/.agents/skills/review/SKILL.md",
      source: "project",
      root: "/workspace/.agents/skills",
    }];
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(skills), { status: 200 }),
    );
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.listSkills("project/one")).resolves.toEqual(skills);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/skills?engagement_id=project%2Fone",
    );
  });

  it("loads project and managed shared skill roots", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      project_root: "/workspace/.agents/skills",
      managed_root: "/data/.agents/skills",
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.getSkillCatalog("project/one")).resolves.toEqual({
      projectRoot: "/workspace/.agents/skills",
      managedRoot: "/data/.agents/skills",
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/skills/catalog?engagement_id=project%2Fone",
    );
  });

  it("loads provider-native hooks and serializes exact selected ids", async () => {
    const hook = {
      id: "audit",
      source: "project",
      path: "/workspace/.agents/hooks/audit",
      manifest: {
        version: 1,
        name: "Audit lifecycle",
        description: "Records outcomes.",
        events: ["chat.turn.started"],
        command: ["run.sh"],
        timeout_seconds: 10,
        side_effects: "workspace",
        failure_policy: "block",
      },
      manifest_sha256: "a".repeat(64),
      executable_sha256: "b".repeat(64),
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify([hook]), { status: 200 }),
    );
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.listNativeHooks("project/one")).resolves.toMatchObject([{
      id: "audit",
      manifest: { timeoutSeconds: 10, sideEffects: "workspace", failurePolicy: "block" },
    }]);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/hooks?engagement_id=project%2Fone",
    );
    expect(chatRequestBody({
      providerId: "provider",
      hookIds: ["audit"],
      messages: [{ role: "user", content: "Run it" }],
    }, true).hook_ids).toEqual(["audit"]);
    // An unused hook selection is omitted so an older remote Core, which forbids
    // unknown request fields, still accepts every chat.
    expect(chatRequestBody({
      providerId: "provider",
      messages: [{ role: "user", content: "Run it" }],
    }, true)).not.toHaveProperty("hook_ids");
  });

  it("carries an operator's reasoning level and reads back the saved one", () => {
    expect(chatRequestBody({
      providerId: "provider",
      reasoningEffort: "none",
      messages: [{ role: "user", content: "Answer" }],
    }, true).reasoning_effort).toBe("none");
    // Absent means the model's own default; an older remote Core forbids
    // unknown request fields, so the key is left out rather than sent null.
    expect(chatRequestBody({
      providerId: "provider",
      messages: [{ role: "user", content: "Answer" }],
    }, true)).not.toHaveProperty("reasoning_effort");
  });

  it("sends a harness chat's subagent model only with delegation on, and reads it back", async () => {
    expect(chatRequestBody({
      backend: "harness",
      harnessProfileId: "codex",
      allowSubagents: true,
      subagentProviderId: "openrouter",
      subagentModel: "deepseek/deepseek-v3.2",
      messages: [{ role: "user", content: "Split it" }],
    }, true)).toMatchObject({
      allow_subagents: true,
      subagent_provider_id: "openrouter",
      subagent_model: "deepseek/deepseek-v3.2",
    });
    // Without delegation the model is left out, so an older remote Core that
    // forbids unknown request fields still accepts the turn.
    const off = chatRequestBody({
      backend: "harness",
      harnessProfileId: "codex",
      subagentProviderId: "openrouter",
      subagentModel: "deepseek/deepseek-v3.2",
      messages: [{ role: "user", content: "Just answer" }],
    }, true);
    expect(off).not.toHaveProperty("subagent_provider_id");
    expect(off).not.toHaveProperty("allow_subagents");

    const wire = {
      created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T10:00:00Z", revision: 2,
      id: "session-1", engagement_id: "engagement-1", title: "Codex chat", backend: "harness",
      harness_profile_id: "codex", model: "gpt-5.5",
      metadata: { provider_subagent: { provider_profile_id: "openrouter", model: "deepseek/deepseek-v3.2" } },
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify([wire, {
      ...wire, id: "session-2", metadata: { provider_subagent: null },
    }]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    const { items } = await client.listChatSessions("engagement-1");
    expect(items[0]).toMatchObject({ allowSubagents: true, subagentProviderId: "openrouter", subagentModel: "deepseek/deepseek-v3.2" });
    expect(items[1]).toMatchObject({ allowSubagents: false });
    expect(items[1].subagentModel).toBeUndefined();
  });

  it("sends a subagent limit only when the operator set one, and reads it back", async () => {
    const body = (overrides: Record<string, unknown>) => chatRequestBody({
      providerId: "provider",
      messages: [{ role: "user", content: "Fan out" }],
      ...overrides,
    }, true);
    expect(body({ allowSubagents: true, maxActiveSubagents: 3 }).max_active_subagents).toBe(3);
    // No limit, or no delegation, leaves the key out for older remote Cores.
    expect(body({ allowSubagents: true })).not.toHaveProperty("max_active_subagents");
    expect(body({ maxActiveSubagents: 3 })).not.toHaveProperty("max_active_subagents");

    const wire = {
      created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T10:00:00Z", revision: 2,
      id: "provider-chat", engagement_id: "engagement-1", title: "Provider chat", backend: "provider",
      provider_profile_id: "provider", model: "model-a",
      metadata: { allow_subagents: true, max_active_subagents: 4 },
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify([wire, {
      ...wire, id: "harness-chat", backend: "harness", harness_profile_id: "codex",
      metadata: { provider_subagent: { provider_profile_id: "openrouter", model: "deepseek/deepseek-v3.2", max_active: 2 } },
    }, {
      ...wire, id: "unlimited", metadata: { allow_subagents: true, max_active_subagents: null },
    }]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    const { items } = await client.listChatSessions("engagement-1");
    expect(items.map((item) => item.subagentLimit)).toEqual([4, 2, undefined]);
  });

  it("reads and writes Core-owned guide progress and guide probes", async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        id: "guide-progress:local:lifecycle-hooks",
        operator_key: "local",
        guide_id: "lifecycle-hooks",
        status: "in_progress",
        step_index: 2,
        revision: 3,
        completed_at: null,
        created_at: "2026-09-19T10:00:00Z",
        updated_at: "2026-09-19T10:05:00Z",
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        guide_id: "lifecycle-hooks", status: "completed", step_index: 4, revision: 4,
        completed_at: "2026-09-19T10:06:00Z", updated_at: "2026-09-19T10:06:00Z",
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ kind: "hook", paths: [".agents/hooks/audit/hook.json"] }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        filename: "AGENTS.md", present: true, size_bytes: 15, truncated: false, limit_bytes: 65536, error: null,
      }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.listGuideProgress()).resolves.toEqual([{
      guideId: "lifecycle-hooks", status: "in_progress", stepIndex: 2, revision: 3,
      completedAt: undefined, updatedAt: "2026-09-19T10:05:00Z",
    }]);
    await expect(client.saveGuideProgress("lifecycle-hooks", { status: "completed", stepIndex: 4, expectedRevision: 3 }))
      .resolves.toMatchObject({ status: "completed", revision: 4, completedAt: "2026-09-19T10:06:00Z" });
    expect(fetchMock.mock.calls[1][0]).toBe("http://127.0.0.1:8765/api/v1/guides/progress/lifecycle-hooks");
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ status: "completed", step_index: 4, expected_revision: 3 });
    await client.createGuideStarterFiles("project/one", "hook", "audit");
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toEqual({ engagement_id: "project/one", kind: "hook", name: "audit" });
    await expect(client.getProjectInstructionsStatus("project/one")).resolves.toEqual({
      filename: "AGENTS.md", present: true, sizeBytes: 15, truncated: false, limitBytes: 65536, error: undefined,
    });
    expect(fetchMock.mock.calls[3][0]).toBe("http://127.0.0.1:8765/api/v1/project-instructions?engagement_id=project%2Fone");
  });

  it("maps restart recovery without hiding unknown tool outcomes", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "turn-recovery",
      session_id: "session-1",
      revision: 3,
      status: "interrupted",
      approval_id: null,
      harness_turn_id: null,
      tool_call_ids: ["tool-unknown"],
      error: "Core restarted while a tool outcome was unknown.",
      recovery_blocked: true,
      unresolved_tool_call_ids: ["tool-unknown"],
      unresolved_hook_execution_ids: [],
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.getPendingChatTurn("session-1")).resolves.toMatchObject({
      id: "turn-recovery",
      status: "interrupted",
      error: "Core restarted while a tool outcome was unknown.",
      recoveryBlocked: true,
      unresolvedToolCallIds: ["tool-unknown"],
      toolCallIds: ["tool-unknown"],
    });
  });

  it("maps durable provider queue state", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "turn-queued", session_id: "session-1", revision: 1, status: "queued",
      queued_at: "2026-09-24T12:00:00Z", admitted_at: null,
      queue_position: 3, capacity_lane: "direct", tool_call_ids: [],
      recovery_blocked: false, unresolved_tool_call_ids: [], unresolved_hook_execution_ids: [],
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.getPendingChatTurn("session-1")).resolves.toMatchObject({
      id: "turn-queued", status: "queued", queuedAt: "2026-09-24T12:00:00Z",
      queuePosition: 3, capacityLane: "direct",
    });
  });

  it("maps, lists, and reconciles uncertain hook outcomes without replay", async () => {
    const responses = [
      new Response(JSON.stringify({
        id: "turn-recovery", session_id: "session-1", revision: 3,
        status: "interrupted", tool_call_ids: [], recovery_blocked: true,
        unresolved_tool_call_ids: [], unresolved_hook_execution_ids: ["hook-run-1"],
      }), { status: 200 }),
      new Response(JSON.stringify([{
        id: "hook-run-1", hook_id: "audit", event_name: "chat.turn.started",
        status: "interrupted", side_effects: "external",
        started_at: "2026-09-18T12:00:00Z", completed_at: "2026-09-18T12:01:00Z",
        late_outcome_status: "failed", late_outcome_exit_code: 2,
      }]), { status: 200 }),
      new Response(JSON.stringify({
        id: "turn-recovery", session_id: "session-1", revision: 4,
        status: "interrupted", tool_call_ids: [], recovery_blocked: false,
        unresolved_tool_call_ids: [], unresolved_hook_execution_ids: [],
      }), { status: 200 }),
    ];
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => responses.shift()!);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.getPendingChatTurn("session-1")).resolves.toMatchObject({
      recoveryBlocked: true, unresolvedHookExecutionIds: ["hook-run-1"],
    });
    await expect(client.listChatHookExecutions("turn-recovery")).resolves.toMatchObject([{
      hookId: "audit", eventName: "chat.turn.started", sideEffects: "external",
      lateOutcomeStatus: "failed", lateOutcomeExitCode: 2,
    }]);
    await expect(client.reconcileChatHook("turn-recovery", {
      expectedRevision: 3,
      hookExecutionId: "hook-run-1",
      outcome: "complete",
      detail: "Verified externally.",
    })).resolves.toMatchObject({ recoveryBlocked: false });
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toEqual({
      expected_revision: 3,
      hook_execution_id: "hook-run-1",
      outcome: "complete",
      detail: "Verified externally.",
    });
  });

  it("maps a waiting results callback on a pending provider turn", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "turn-callback", session_id: "session-1", revision: 2,
      status: "waiting_callback", tool_call_ids: ["tool-1"],
      recovery_blocked: false, unresolved_tool_call_ids: [], unresolved_hook_execution_ids: [],
      results_url: "http://192.168.1.20:8000/api/v1/automation-processes/proc-1/results",
      process_id: "proc-1",
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    await expect(client.getPendingChatTurn("session-1")).resolves.toMatchObject({
      status: "waiting_callback",
      resultsUrl: "http://192.168.1.20:8000/api/v1/automation-processes/proc-1/results",
      processId: "proc-1",
    });
  });

  it("loads session-scoped hook outcomes for the latest completed turn", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify([{
      id: "hook-run-2", hook_id: "audit", event_name: "chat.turn.completed",
      status: "complete", side_effects: "none",
      started_at: "2026-09-18T12:00:00Z", completed_at: "2026-09-18T12:00:01Z",
    }]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.listSessionHookExecutions("session/one")).resolves.toMatchObject([{
      hookId: "audit", eventName: "chat.turn.completed", status: "complete",
    }]);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/chat/sessions/session%2Fone/hooks",
    );
  });

  it("submits an exact operator reconciliation without replaying the tool", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "turn-recovery",
      session_id: "session-1",
      revision: 4,
      status: "interrupted",
      tool_call_ids: ["tool-unknown"],
      recovery_blocked: false,
      unresolved_tool_call_ids: [],
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.reconcileChatTool("turn-recovery", {
      expectedRevision: 3,
      toolCallId: "tool-unknown",
      outcome: "complete",
      detail: "Verified in the target system.",
    })).resolves.toMatchObject({ revision: 4, recoveryBlocked: false });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/chat/turns/turn-recovery/reconcile-tool",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      expected_revision: 3,
      tool_call_id: "tool-unknown",
      outcome: "complete",
      detail: "Verified in the target system.",
    });
  });
  it("loads the exact approval independently of the pending catalog", async () => {
    const approval = { id: "approval/one", status: "pending", exact_request: { tool_name: "read_file", arguments: { path: "notes.txt" }, cwd: "/workspace", argv: ["read", "notes.txt"] } };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(approval), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    const controller = new AbortController();
    expect(await client.getApproval(approval.id, controller.signal)).toEqual(approval);
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8765/api/v1/approvals/approval%2Fone");
    expect(fetchMock.mock.calls[0][1]?.signal).toBeDefined();
  });

  it("pins requests to /api/v1 and authenticates with the configured token", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "ready" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const client = new ApiClient({
      baseUrl: "http://127.0.0.1:8765",
      token: "one-time-token",
      fetch: fetchMock,
    });

    const health = await client.health();

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/api/v1/health");
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer one-time-token");
    expect(health.runner).toBe("ready");
  });

  it("recovers an active Project terminal with its exact runtime snapshot", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      active: true,
      session: {
        session_id: "terminal-1",
        created_at: "2026-07-15T12:00:00Z",
        websocket_ticket: "fresh-ticket",
        ticket_expires_at: "2026-07-13T18:00:00Z",
        websocket_path: "/api/v1/container-terminals/terminal-1/ws",
        reconnect_grace_seconds: 600,
        replay_max_bytes: 1_048_576,
        last_sequence: 0,
      },
      runtime: {
        source_image: "docker.io/kalilinux/kali-rolling:latest",
        base_image: `docker.io/kalilinux/kali-rolling@sha256:${"b".repeat(64)}`,
        base_image_digest: `sha256:${"b".repeat(64)}`,
        image: `sha256:${"c".repeat(64)}`,
        image_digest: `sha256:${"c".repeat(64)}`,
        installed_packages: ["kali-linux-headless", "iputils-ping"],
        interpreter: "/bin/bash",
        arguments: ["--noprofile", "--norc", "-i"],
        runner_profile_id: "local",
        runner_profile_revision: 1,
        runner_runtime: "podman",
        runner_isolation: "rootless",
        runner_executable: "/usr/bin/podman",
        runner_platform: "linux/amd64",
        runner_context: null,
      },
    }), { status: 200 }));
    const client = new ApiClient({
      baseUrl: "http://127.0.0.1:8765",
      token: "test-token",
      fetch: fetchMock,
    });

    const recovered = await client.recoverContainerTerminal("project/one");

    expect(recovered).toMatchObject({
      active: true,
      session: {
        sessionId: "terminal-1",
        createdAt: "2026-07-15T12:00:00Z",
        websocketTicket: "fresh-ticket",
        lastSequence: 0,
      },
      runtime: {
        imageDigest: `sha256:${"c".repeat(64)}`,
        runnerRuntime: "podman",
      },
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/api/v1/engagements/project%2Fone/container-terminal/recover");
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer test-token");
  });

  it("maps multi-terminal recovery, capacity, and targeted close contracts", async () => {
    const runtime = {
      source_image: "docker.io/kalilinux/kali-rolling:latest",
      base_image: `docker.io/kalilinux/kali-rolling@sha256:${"b".repeat(64)}`,
      base_image_digest: `sha256:${"b".repeat(64)}`,
      image: `sha256:${"c".repeat(64)}`,
      image_digest: `sha256:${"c".repeat(64)}`,
      installed_packages: ["kali-linux-headless", "iputils-ping"],
      interpreter: "/bin/bash",
      arguments: ["--noprofile", "--norc", "-i"],
      runner_profile_id: "local",
      runner_profile_revision: 1,
      runner_runtime: "podman",
      runner_isolation: "rootless",
      runner_executable: "/usr/bin/podman",
      runner_platform: "linux/amd64",
    };
    const session = {
      session_id: "terminal-1",
      created_at: "2026-07-15T12:00:00Z",
      websocket_ticket: "fresh-ticket",
      ticket_expires_at: "2026-07-15T18:00:00Z",
      websocket_path: "/api/v1/container-terminals/terminal-1/ws",
      reconnect_grace_seconds: 600,
      replay_max_bytes: 1_048_576,
      last_sequence: 0,
    };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/container-terminals/recover")) {
        return new Response(JSON.stringify({ sessions: [{ session, runtime }] }), { status: 200 });
      }
      if (path.endsWith("/container-terminal/capacity")) {
        return new Response(JSON.stringify({ active_sessions: 1, available_sessions: 31, max_active_sessions: 32 }), { status: 200 });
      }
      if (path.endsWith("/container-terminals/terminal-1") && init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return new Response(JSON.stringify({ detail: "not mocked" }), { status: 500 });
    });
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", token: "test-token", fetch: fetchMock });

    const recovered = await client.recoverContainerTerminals("project/one");
    const currentCapacity = await client.containerTerminalCapacity();
    await client.closeContainerTerminal("terminal-1");

    expect(recovered.sessions[0]).toMatchObject({
      session: { sessionId: "terminal-1", createdAt: "2026-07-15T12:00:00Z" },
      runtime: { runnerRuntime: "podman" },
    });
    expect(currentCapacity).toEqual({ activeSessions: 1, availableSessions: 31, maxActiveSessions: 32 });
    expect(fetchMock.mock.calls.map(([input, init]) => [String(input), init?.method ?? "GET"])).toEqual([
      ["http://127.0.0.1:8765/api/v1/engagements/project%2Fone/container-terminals/recover", "POST"],
      ["http://127.0.0.1:8765/api/v1/container-terminal/capacity", "GET"],
      ["http://127.0.0.1:8765/api/v1/container-terminals/terminal-1", "DELETE"],
    ]);
  });

  it("preserves structured API failures and request IDs", async () => {
    const client = new ApiClient({
      fetch: vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ message: "Approval expired" }), {
          status: 409,
          headers: { "x-request-id": "request-42" },
        }),
      ),
    });

    const error = await client.decideApproval("approval-1", { decision: "approve" }).catch((value) => value);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      requestId: "request-42",
      message: "Approval expired Reference: request-42.",
    });
  });

  it("maps the active terminal container public IP status", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      address: "203.0.113.42",
      observed_at: "2026-09-04T10:40:00Z",
      stale: false,
    }), { status: 200, headers: { "content-type": "application/json" } }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", token: "test-token", fetch: fetchMock });

    await expect(client.engagementContainerTerminalPublicIp("project/one")).resolves.toEqual({
      address: "203.0.113.42",
      observedAt: "2026-09-04T10:40:00Z",
      stale: false,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/v1/engagements/project%2Fone/container-terminal/public-ip",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("summarizes validation arrays without copying oversized inputs into the error", async () => {
    const client = new ApiClient({
      fetch: vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({
          detail: [{
            type: "string_too_long",
            loc: ["response", "summary"],
            msg: "String should have at most 4000 characters",
            input: "x".repeat(8_000),
          }],
        }), { status: 422, headers: { "x-request-id": "req_validation" } }),
      ),
    });

    const error = await client.health().catch((value) => value);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe(
      "The supplied summary exceeded its validated length limit. Reference: req_validation.",
    );
    expect(error.message).not.toContain("x".repeat(100));
  });

  it("preserves the Core diagnosis instead of inventing an interface error", async () => {
    const envelope = {
      detail: "Harness transport failed.",
      code: "harness_stream_failed",
      feature: "harnesses",
      request_id: "req_harness_shared",
      operation_id: "op_harness_shared",
      error_id: "err_harness_shared",
      retryable: true,
      reason_code: "transport_closed",
      operator_detail: "Codex app-server closed stdout before turn completion.",
      impact: "The harness turn did not complete.",
      remediation_id: "harnesses.transport_closed",
      help_article: "harnesses",
      recovery_action: "Retry this operation",
      recovery_destination: "/settings#harnesses-settings",
    };
    const client = new ApiClient({
      fetch: vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify(envelope), { status: 502 }),
      ),
    });

    const error = await client.health().catch((value) => value);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 502,
      requestId: "req_harness_shared",
      operationId: "op_harness_shared",
      errorId: "err_harness_shared",
      reasonCode: "transport_closed",
      operatorDetail: envelope.operator_detail,
      impact: envelope.impact,
      remediationId: "harnesses.transport_closed",
      recoveryAction: "Retry this operation",
      recoveryDestination: "/settings#harnesses-settings",
    });
  });

  it("maps zero-setup readiness and refreshes runtime detection idempotently", async () => {
    const status = {
      application_stage: "ready",
      stage_detail: "Terminal is ready.",
      stage_started_at: "2026-07-17T10:00:00Z",
      retryable: false,
      recovery_actions: [],
      core: { status: "degraded", detail: "A model is optional" },
      scratch_project_id: "scratch-project",
      terminal: {
        status: "ready",
        runner_profile_id: "runner-local",
        candidates: [{
          candidate_id: `fixed:${"a".repeat(32)}`,
          runner_profile_id: "runner-local",
          source: "detected",
          name: "Local Podman",
          runtime: "podman",
          executable: "/usr/bin/podman",
          context: null,
          platform: "linux/amd64",
          isolation: "rootless",
          healthy: true,
          detail: null,
        }],
        image_preparation: {
          phase: "ready",
          operation_id: "00000000-0000-4000-8000-000000000001",
          project_id: "scratch-project",
          progress_percent: 100,
          progress_indeterminate: false,
          can_cancel: false,
          can_retry: false,
          image_digest: `sha256:${"b".repeat(64)}`,
          started_at: "2026-07-13T18:00:00Z",
          completed_at: "2026-07-13T18:01:00Z",
          detail: "Cached and verified",
        },
        detail: null,
      },
      assistant: { status: "needs_model", provider_profile_id: null, detail: null },
    };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () =>
      new Response(JSON.stringify(status), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const initial = await client.setupStatus();
    const refreshed = await client.refreshSetupRuntime();

    expect(initial).toMatchObject({
      applicationStage: "ready",
      stageDetail: "Terminal is ready.",
      core: { status: "degraded", detail: "A model is optional" },
      scratchProjectId: "scratch-project",
      terminal: {
        status: "ready",
        runnerProfileId: "runner-local",
        candidates: [expect.objectContaining({ candidateId: `fixed:${"a".repeat(32)}`, name: "Local Podman", healthy: true })],
        imagePreparation: expect.objectContaining({ phase: "ready", progressPercent: 100, canCancel: false }),
      },
      assistant: { status: "needs_model" },
    });
    expect(refreshed).toEqual(initial);
    expect(fetchMock.mock.calls.map(([input, init]) => [String(input), init?.method ?? "GET"])).toEqual([
      ["http://127.0.0.1:8765/api/v1/setup/status", "GET"],
      ["http://127.0.0.1:8765/api/v1/setup/runtime/refresh", "POST"],
    ]);
  });

  it("maps idempotent setup control operations without losing preparation state", async () => {
    const status = {
      core: { status: "ready", detail: null },
      scratch_project_id: "scratch-project",
      terminal: {
        status: "preparing_image",
        runner_profile_id: "runner-local",
        candidates: [],
        image_preparation: {
          phase: "preparing_image",
          operation_id: "00000000-0000-4000-8000-000000000001",
          project_id: "scratch-project",
          progress_percent: 42,
          progress_indeterminate: false,
          can_cancel: true,
          can_retry: false,
          detail: "Preparing the workstation image",
        },
        detail: "Preparing the workstation image",
      },
      assistant: { status: "needs_model", provider_profile_id: null, detail: null },
    };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const pathname = new URL(String(input)).pathname;
      const operation = pathname.endsWith("/runtime/select")
        ? "runner_selection"
        : pathname.endsWith("/image/retry")
          ? "image_preparation_retry"
          : pathname.endsWith("/image/cancel")
            ? "image_preparation_cancellation"
            : "image_preparation";
      return new Response(JSON.stringify({
        operation,
        accepted: true,
        idempotent: false,
        operation_id: "00000000-0000-4000-8000-000000000001",
        setup: status,
      }), { status: 200 });
    });
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const selected = await client.selectSetupRuntime(`fixed:${"a".repeat(32)}`);
    const prepared = await client.prepareSetupImage("scratch-project");
    await client.retrySetupImage("scratch-project");
    await client.cancelSetupImage("00000000-0000-4000-8000-000000000001");

    expect(selected.operation).toBe("runner_selection");
    expect(prepared.setup.terminal.imagePreparation).toMatchObject({
      phase: "preparing_image",
      progressPercent: 42,
      canCancel: true,
    });
    expect(fetchMock.mock.calls.map(([input, init]) => [
      new URL(String(input)).pathname,
      JSON.parse(String(init?.body)),
    ])).toEqual([
      ["/api/v1/setup/runtime/select", { candidate_id: `fixed:${"a".repeat(32)}` }],
      ["/api/v1/setup/image/prepare", { project_id: "scratch-project" }],
      ["/api/v1/setup/image/retry", { project_id: "scratch-project" }],
      ["/api/v1/setup/image/cancel", { operation_id: "00000000-0000-4000-8000-000000000001" }],
    ]);
  });

  it("streams raw workspace uploads with an explicit overwrite decision", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      engagement_id: "project-1",
      path: "notes/proof.txt",
      size: 5,
      sha256: "a".repeat(64),
      overwritten: true,
    }), { status: 201 }));
    const client = new ApiClient({
      baseUrl: "http://127.0.0.1:8765",
      token: "local-token",
      fetch: fetchMock,
    });
    const file = new Blob(["proof"], { type: "text/plain" });

    const expectedSha256 = "b".repeat(64);
    const result = await client.uploadWorkspaceFile("project-1", "notes/proof.txt", file, true, undefined, expectedSha256);

    expect(result).toMatchObject({ path: "notes/proof.txt", size: 5, overwritten: true });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("http://127.0.0.1:8765/api/v1/engagements/project-1/workspace/file?path=notes%2Fproof.txt&overwrite=true");
    expect(init?.method).toBe("PUT");
    expect(init?.body).toBe(file);
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/octet-stream");
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer local-token");
    expect(new Headers(init?.headers).get("If-Match")).toBe(expectedSha256);
  });

  it("maps snake_case Core arrays into engagement and run summaries", async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        id: "engagement-1",
        name: "ACME External",
        client_name: "ACME",
        status: "active",
        created_at: "2026-07-12T10:00:00Z",
        updated_at: "2026-07-12T11:00:00Z",
        revision: 1,
        metadata: { scope_asset_count: 12 },
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        id: "run-1",
        engagement_id: "engagement-1",
        objective: "Validate external services",
        status: "running",
        created_at: "2026-07-12T10:00:00Z",
        updated_at: "2026-07-12T11:30:00Z",
        started_at: "2026-07-12T10:05:00Z",
        completed_at: null,
        revision: 1,
        metadata: { completed_tasks: 2, total_tasks: 5, spent_usd: 1.25 },
      }]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const engagements = await client.listEngagements();
    const runs = await client.listRuns("engagement-1");

    expect(engagements).toEqual({
      items: [expect.objectContaining({
        id: "engagement-1",
        clientName: "ACME",
        updatedAt: "2026-07-12T11:00:00Z",
        scopeAssetCount: 12,
      })],
      total: 1,
    });
    expect(runs.items[0]).toMatchObject({
      engagementId: "engagement-1",
      title: "Validate external services",
      completedTasks: 2,
      totalTasks: 5,
      spentUsd: 1.25,
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/engagements?limit=1000&offset=0",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8765/api/v1/runs?engagement_id=engagement-1&limit=1000&offset=0",
    );
  });

  it("maps automatic Mission restart recovery state", async () => {
    const run = {
      id: "run-recovery",
      engagement_id: "engagement-1",
      objective: "Apply change",
      status: "running",
      created_at: "2026-07-12T10:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 4,
      metadata: {
        restart_recovery: {
          required: false,
          automatic: true,
          state: "running",
          reason: "Core restarted",
          unresolved_tool_call_ids: [],
          auto_continued_unknown_tool_call_ids: ["call-1"],
          effects: [{
            tool_call_id: "call-1",
            tool_name: "write_file",
            risk_class: "workspace_write",
            status_at_restart: "running",
          }],
        },
      },
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify([run]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const listed = await client.listRuns("engagement-1");
    expect(listed.items[0]).toMatchObject({
      revision: 4,
      restartRecovery: {
        required: false,
        automatic: true,
        state: "running",
        unresolvedToolCallIds: [],
        effects: [{ toolCallId: "call-1", toolName: "write_file" }],
      },
    });
  });

  it("deletes a mission through the dedicated run endpoint", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await client.deleteRun("run/one");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/runs/run%2Fone",
    );
    expect(fetchMock.mock.calls[0][1]?.method).toBe("DELETE");
  });

  it("loads only pending approvals and sends edited_arguments on decisions", async () => {
    const approval = {
      id: "approval-1",
      engagement_id: "engagement-1",
      run_id: "run-1",
      status: "pending",
      risk_class: "active_scan",
      exact_request: {
        tool_name: "scan.tcp",
        arguments: { ports: [80] },
        argv: ["/usr/bin/nmap", "-sT", "-p", "80", "192.0.2.8"],
        image: `example.invalid/nmap@sha256:${"a".repeat(64)}`,
        runtime_digest: "sha256:" + "b".repeat(64),
      },
      target: "192.0.2.8",
      credential_class: "lab-read-only",
      expected_effects: ["Probe one in-scope target"],
      policy_rationale: "An active scan needs operator approval",
      requested_by: "network-specialist",
      requested_at: "2026-07-12T11:00:00Z",
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify([
        approval,
        { ...approval, id: "approval-2", status: "rejected" },
      ]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ...approval,
        status: "edited",
        exact_request: { ...approval.exact_request, arguments: { ports: [443] } },
      }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const pending = await client.listApprovals("engagement-1");
    const decided = await client.decideApproval("approval-1", {
      decision: "approve",
      reason: "HTTPS only",
      editedArguments: { ports: [443] },
    });

    expect(pending.items).toEqual([expect.objectContaining({
      id: "approval-1",
      risk: "active",
      toolName: "scan.tcp",
      expectedEffects: "Probe one in-scope target",
      command: ["/usr/bin/nmap", "-sT", "-p", "80", "192.0.2.8"],
      runtimeDigest: "sha256:" + "b".repeat(64),
      credentialClass: "lab-read-only",
    })]);
    expect(decided).toMatchObject({ status: "approved", arguments: { ports: [443] } });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/approvals?engagement_id=engagement-1&limit=1000&offset=0",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      decision: "approve",
      reason: "HTTPS only",
      edited_arguments: { ports: [443] },
    });
  });

  it("loads global provider profiles without an engagement filter", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify([{
      id: "provider-vllm",
      name: "Lab vLLM",
      provider_type: "vllm",
      enabled: true,
      is_local: true,
      model_allowlist: ["security-model"],
      capabilities: { streaming: true, tool_calling: true, vision: false },
      privacy: { local_only: true, residency: [] },
      metadata: {},
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    }]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const providers = await client.listProviders();

    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8765/api/v1/providers?limit=1000&offset=0");
    expect(providers.items[0]).toMatchObject({
      name: "Lab vLLM",
      kind: "local",
      modelCount: 1,
      privacy: "local_only",
      capabilities: ["streaming", "tool calling"],
    });
  });

  it("starts every generic workspace list with Core's bounded page size", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () =>
      new Response(JSON.stringify([]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await Promise.all([
      client.listEngagements(),
      client.listOperatorProfiles(),
      client.listRuns("engagement/one"),
      client.listApprovals("engagement/one"),
      client.listAssets("engagement/one"),
      client.listFindings("engagement/one"),
      client.listEvidence("engagement/one"),
      client.listReports("engagement/one"),
      client.listProviders(),
      client.listKnowledgeSources("engagement/one"),
      client.listChatSessions("engagement/one"),
    ]);

    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:8765/api/v1/engagements?limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/operator-profiles",
      "http://127.0.0.1:8765/api/v1/runs?engagement_id=engagement%2Fone&limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/approvals?engagement_id=engagement%2Fone&limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/assets?engagement_id=engagement%2Fone&limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/findings?engagement_id=engagement%2Fone&limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/evidence?engagement_id=engagement%2Fone&limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/reports?engagement_id=engagement%2Fone&limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/providers?limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/knowledge?engagement_id=engagement%2Fone&limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/chat-sessions?engagement_id=engagement%2Fone&limit=1000&offset=0",
    ]);
  });

  it("paginates generic lists past Core's 1000-record page boundary", async () => {
    const rows = Array.from({ length: 1001 }, (_, index) => ({
      id: `engagement-${index}`,
      name: `Engagement ${index}`,
      description: "",
      client_name: null,
      status: "active",
      tags: [],
      metadata: {},
      created_at: "2026-07-12T10:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    }));
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = new URL(String(input));
      const offset = Number(url.searchParams.get("offset") ?? 0);
      return new Response(JSON.stringify(rows.slice(offset, offset + 1000)), { status: 200 });
    });
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const engagements = await client.listEngagements();

    expect(engagements.total).toBe(1001);
    expect(engagements.items.at(-1)?.id).toBe("engagement-1000");
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:8765/api/v1/engagements?limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/engagements?limit=1000&offset=1000",
    ]);
  });

  it("refreshes vLLM health and maps dynamically served models", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      provider_id: "provider-vllm",
      healthy: true,
      models: ["security-model", "vision-model"],
      model_descriptors: [{
        id: "security-model",
        name: "Security Model",
        description: null,
        canonical_slug: null,
        context_window: 32_768,
        max_output_tokens: 4_096,
        input_modalities: ["text"],
        output_modalities: ["text"],
        supported_parameters: ["tools"],
        pricing: {},
      }],
      unlisted_models: ["coder-model"],
      unlisted_model_descriptors: [{ id: "coder-model", name: "Coder Model", context_window: 131_072 }],
      detail: null,
      provider_revision: 7,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const health = await client.refreshProviderHealth("provider-vllm");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/providers/provider-vllm/health",
    );
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
    expect(health).toEqual({
      providerId: "provider-vllm",
      healthy: true,
      models: ["security-model", "vision-model"],
      modelDescriptors: [{
        id: "security-model",
        name: "Security Model",
        description: null,
        canonicalSlug: null,
        contextWindow: 32_768,
        maxOutputTokens: 4_096,
        inputModalities: ["text"],
        outputModalities: ["text"],
        supportedParameters: ["tools"],
        pricing: {},
      }],
      unlistedModels: ["coder-model"],
      unlistedModelDescriptors: [{
        id: "coder-model",
        name: "Coder Model",
        description: null,
        canonicalSlug: null,
        contextWindow: 131_072,
        maxOutputTokens: null,
        inputModalities: [],
        outputModalities: [],
        supportedParameters: [],
        pricing: {},
      }],
      detail: undefined,
      providerRevision: 7,
    });
  });

  it("discovers the provider catalog and creates a local vLLM profile", async () => {
    const provider = {
      id: "provider-vllm",
      name: "Local vLLM",
      provider_type: "vllm",
      endpoint: "http://127.0.0.1:8000/v1",
      enabled: true,
      is_local: true,
      model_allowlist: ["security-model"],
      capabilities: { streaming: true },
      privacy: { local_only: true, residency: [] },
      metadata: { default_model: "security-model" },
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        flavor: "vllm",
        adapter: "openai_compatible",
        display_name: "vLLM",
        local: true,
        default_base_url: "http://127.0.0.1:8000/v1",
        support_tier: "compatible",
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(provider), { status: 201 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const catalog = await client.listProviderCatalog();
    const created = await client.createProvider({
      name: "Local vLLM",
      providerType: "vllm",
      endpoint: catalog[0].defaultBaseUrl,
      local: true,
      defaultModel: "security-model",
    });

    expect(catalog[0]).toMatchObject({
      flavor: "vllm",
      displayName: "vLLM",
      local: true,
      defaultBaseUrl: "http://127.0.0.1:8000/v1",
    });
    expect(fetchMock.mock.calls[1][0]).toBe("http://127.0.0.1:8765/api/v1/providers");
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({
      provider_type: "vllm",
      endpoint: "http://127.0.0.1:8000/v1",
      is_local: true,
      model_allowlist: [],
      privacy: { local_only: true },
    });
    expect(created).toMatchObject({ name: "Local vLLM", kind: "local" });
  });

  it("maps OrcaRouter profiles as gateway providers", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify([{
      id: "provider-orcarouter",
      name: "OrcaRouter",
      provider_type: "orcarouter",
      endpoint: "https://api.orcarouter.ai/v1",
      enabled: true,
      is_local: false,
      secret_ref: "env:ORCAROUTER_API_KEY",
      model_allowlist: ["orcarouter/auto"],
      capabilities: { streaming: true, tool_calling: true },
      privacy: { local_only: false, residency: [] },
      metadata: { default_model: "orcarouter/auto" },
      created_at: "2026-09-04T10:00:00Z",
      updated_at: "2026-09-04T10:00:00Z",
      revision: 1,
    }]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const providers = await client.listProviders();

    expect(providers.items[0]).toMatchObject({
      id: "provider-orcarouter",
      providerType: "orcarouter",
      kind: "gateway",
      credentialEnv: "ORCAROUTER_API_KEY",
      defaultModel: "orcarouter/auto",
    });
  });

  it("maps fixed-loopback local provider discovery", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify([{
      flavor: "ollama",
      display_name: "Ollama",
      endpoint: "http://127.0.0.1:11434/v1",
      models: ["qwen2.5-coder"],
    }]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.discoverLocalProviders()).resolves.toEqual([{
      flavor: "ollama",
      displayName: "Ollama",
      endpoint: "http://127.0.0.1:11434/v1",
      models: ["qwen2.5-coder"],
    }]);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/v1/providers/discover-local",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("persists only provider credential references and explicit document-data permission", async () => {
    const provider = {
      id: "provider-openai",
      name: "OpenAI review",
      provider_type: "openai",
      endpoint: "https://api.openai.com/v1",
      enabled: true,
      is_local: false,
      secret_ref: "env:OPENAI_API_KEY",
      model_allowlist: ["gpt-5-mini"],
      capabilities: { streaming: true },
      privacy: { local_only: false, residency: [], permits_sensitive_data: true },
      metadata: { default_model: "gpt-5-mini" },
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(provider), { status: 201 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const created = await client.createProvider({
      name: "OpenAI review",
      providerType: "openai",
      endpoint: "https://api.openai.com/v1",
      local: false,
      defaultModel: "gpt-5-mini",
      credentialEnv: "env:OPENAI_API_KEY",
      permitsSensitiveData: true,
    });

    const request = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(request.secret_ref).toBe("env:OPENAI_API_KEY");
    expect(request.privacy).toEqual({ local_only: false, permits_sensitive_data: true, auto_share_tool_results: false });
    expect(JSON.stringify(request)).not.toContain("sk-");
    expect(created).toMatchObject({
      credentialEnv: "OPENAI_API_KEY",
      defaultModel: "gpt-5-mini",
      permitsSensitiveData: true,
      models: ["gpt-5-mini"],
    });
  });

  it("serializes Vertex and Bedrock runtime options into provider metadata", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (_input, init) => {
      const body = JSON.parse(String(init?.body));
      return new Response(JSON.stringify({
        id: `provider-${body.provider_type}`,
        name: body.name,
        provider_type: body.provider_type,
        endpoint: body.endpoint,
        enabled: true,
        is_local: false,
        secret_ref: body.secret_ref,
        model_allowlist: body.model_allowlist,
        capabilities: body.capabilities,
        privacy: body.privacy,
        metadata: body.metadata,
        created_at: "2026-07-12T11:00:00Z",
        updated_at: "2026-07-12T11:00:00Z",
        revision: 1,
      }), { status: 201 });
    });
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await client.createProvider({ name: "Vertex", providerType: "vertex", endpoint: "https://us-central1-aiplatform.googleapis.com", local: false, defaultModel: "gemini-2.5-pro", credentialEnv: "GOOGLE_ACCESS_TOKEN", options: { project: "security-project", location: "us-central1" } });
    await client.createProvider({ name: "Bedrock", providerType: "bedrock", endpoint: "https://bedrock-runtime.amazonaws.com", local: false, defaultModel: "anthropic.claude", options: { region: "us-east-1" } });

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).metadata.options).toEqual({ project: "security-project", location: "us-central1" });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body)).metadata.options).toEqual({ region: "us-east-1" });
  });

  it("creates manual findings as unverified candidates with normalized references", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (_input, init) => {
      const body = JSON.parse(String(init?.body));
      return new Response(JSON.stringify({
        id: "finding-new",
        ...body,
        created_at: "2026-07-12T11:00:00Z",
        updated_at: "2026-07-12T11:00:00Z",
        revision: 1,
      }), { status: 201 });
    });
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const created = await client.createFinding({
      engagementId: "engagement-1",
      title: "  Reflected script injection  ",
      description: "  Reflected in the search response.  ",
      severity: "high",
      severityRationale: "  Internet reachable.  ",
      assetIds: ["asset-1", "asset-1"],
      evidenceIds: ["evidence-1", "evidence-1"],
      cveIds: ["cve-2026-1234"],
      cweIds: ["cwe-79", "CWE-79"],
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/api/v1/findings");
    expect(JSON.parse(String(init?.body))).toEqual({
      engagement_id: "engagement-1",
      title: "Reflected script injection",
      description: "Reflected in the search response.",
      status: "candidate",
      severity: "high",
      severity_rationale: "Internet reachable.",
      asset_ids: ["asset-1"],
      evidence_ids: ["evidence-1"],
      cve_ids: ["CVE-2026-1234"],
      cwe_ids: ["CWE-79"],
      metadata: { origin: "manual_operator_entry" },
    });
    expect(created).toMatchObject({ id: "finding-new", status: "candidate", verifierId: undefined, evidenceCount: 1 });
  });

  it("updates every editable finding field with normalized, revision-checked changes", async () => {
    const finding = {
      id: "finding-1",
      engagement_id: "engagement-1",
      title: "Updated finding",
      description: "Updated description",
      severity: "critical" as const,
      severity_rationale: "Material external impact",
      status: "accepted-risk",
      asset_ids: ["asset-1", "asset-2"],
      evidence_ids: ["evidence-1", "evidence-2"],
      cve_ids: ["CVE-2026-1234"],
      cwe_ids: ["CWE-79"],
      verifier_id: null,
      verified_at: null,
      created_at: "2026-07-12T10:00:00Z",
      updated_at: "2026-07-12T12:00:00Z",
      revision: 4,
    };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (_input, init) => {
      const request = JSON.parse(String(init?.body));
      return new Response(JSON.stringify({ ...finding, ...request.changes }), { status: 200 });
    });
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const updated = await client.updateFinding("finding/1", {
      title: "  Updated finding  ",
      description: "  Updated description  ",
      severity: "critical",
      severityRationale: "  Material external impact  ",
      assetIds: ["asset-1", "asset-1", "asset-2"],
      cveIds: ["cve-2026-1234", "CVE-2026-1234"],
      cweIds: ["cwe-79", "CWE-79"],
      status: "accepted_risk",
      evidenceIds: ["evidence-1", "evidence-1", "evidence-2"],
      expectedRevision: 3,
    });

    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8765/api/v1/findings/finding%2F1");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      expected_revision: 3,
      changes: {
        title: "Updated finding",
        description: "Updated description",
        severity: "critical",
        severity_rationale: "Material external impact",
        asset_ids: ["asset-1", "asset-2"],
        cve_ids: ["CVE-2026-1234"],
        cwe_ids: ["CWE-79"],
        status: "accepted-risk",
        evidence_ids: ["evidence-1", "evidence-2"],
      },
    });
    expect(updated).toMatchObject({
      id: "finding-1",
      title: "Updated finding",
      severity: "critical",
      status: "accepted_risk",
      affectedAssetCount: 2,
      evidenceCount: 2,
      revision: 4,
    });
  });

  it("updates, disables, and deletes providers with optimistic revisions", async () => {
    const provider = {
      id: "provider-anthropic",
      name: "Anthropic review",
      provider_type: "anthropic",
      endpoint: "https://api.anthropic.com",
      enabled: true,
      is_local: false,
      secret_ref: "env:ANTHROPIC_API_KEY",
      model_allowlist: ["claude-old"],
      capabilities: { streaming: true },
      privacy: { local_only: false, retention: "provider-policy", residency: ["us"], permits_sensitive_data: false },
      metadata: { default_model: "claude-old", options: { anthropic_version: "2023-06-01", input_cost_per_million: 3 }, managed_note: "preserve" },
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 3,
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockImplementationOnce(async (_input, init) => {
        const changes = JSON.parse(String(init?.body)).changes;
        return new Response(JSON.stringify({ ...provider, ...changes, revision: 4 }), { status: 200 });
      })
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...provider, enabled: false, revision: 5 }), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const updated = await client.updateProvider(provider.id, {
      name: "Anthropic primary",
      providerType: "anthropic",
      endpoint: provider.endpoint,
      local: false,
      defaultModel: "claude-new",
      modelAllowlist: ["claude-old"],
      credentialEnv: "ANTHROPIC_API_KEY",
      permitsSensitiveData: true,
      autoShareToolResults: false,
      retention: "provider-policy",
      residency: ["us"],
      options: { anthropic_version: "2023-06-01", input_cost_per_million: 3 },
      metadata: provider.metadata,
      expectedRevision: 3,
    });
    const updateBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(updateBody).toEqual({
      changes: {
        name: "Anthropic primary",
        endpoint: "https://api.anthropic.com",
        secret_ref: "env:ANTHROPIC_API_KEY",
        model_allowlist: ["claude-new", "claude-old"],
        privacy: { local_only: false, retention: "provider-policy", residency: ["us"], permits_sensitive_data: true, auto_share_tool_results: false },
        metadata: { default_model: "claude-new", options: { anthropic_version: "2023-06-01", input_cost_per_million: 3 }, managed_note: "preserve" },
      },
      expected_revision: 3,
    });
    expect(updated).toMatchObject({ revision: 4, defaultModel: "claude-new", effectiveDefaultModel: "claude-new" });

    await client.setProviderEnabled(provider.id, false, 4);
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ changes: { enabled: false }, expected_revision: 4 });
    await client.deleteProvider(provider.id, 5);
    expect(fetchMock.mock.calls[2][1]?.method).toBe("DELETE");
    expect(new Headers(fetchMock.mock.calls[2][1]?.headers).get("If-Match")).toBe("5");
  });

  it("records standing tool-result consent on the runtime profile", async () => {
    const wire = {
      id: "provider-openai",
      name: "OpenAI",
      provider_type: "openai",
      enabled: true,
      is_local: false,
      model_allowlist: [],
      capabilities: {},
      privacy: { local_only: false, retention: "provider-policy", residency: ["us"], permits_sensitive_data: true, auto_share_tool_results: true },
      metadata: {},
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 4,
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(wire), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const saved = await client.setProviderToolResultSharing({
      id: "provider-openai",
      revision: 3,
      local: false,
      permitsSensitiveData: true,
      retention: "provider-policy",
      residency: ["us"],
    } as unknown as ProviderHealth, true);

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      changes: { privacy: { local_only: false, retention: "provider-policy", residency: ["us"], permits_sensitive_data: true, auto_share_tool_results: true } },
      expected_revision: 3,
    });
    expect(saved.autoShareToolResults).toBe(true);
  });

  it("refuses standing tool-result consent for a text-only profile", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "provider-openai",
      name: "OpenAI",
      provider_type: "openai",
      enabled: true,
      is_local: false,
      model_allowlist: [],
      capabilities: {},
      privacy: { local_only: false, residency: [], permits_sensitive_data: false },
      metadata: {},
      revision: 4,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await client.setProviderToolResultSharing({
      id: "provider-openai",
      revision: 3,
      local: false,
      permitsSensitiveData: false,
      residency: [],
    } as unknown as ProviderHealth, true);

    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body.changes.privacy.auto_share_tool_results).toBe(false);
  });

  it("keeps explicit and fallback provider model semantics distinct", async () => {
    const provider = {
      id: "provider-openai",
      name: "OpenAI",
      provider_type: "openai",
      endpoint: "https://api.openai.com/v1",
      enabled: true,
      is_local: false,
      model_allowlist: ["allowed-first"],
      capabilities: {},
      privacy: { local_only: false, residency: [] },
      metadata: {},
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 2,
    };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (_input, init) => {
      const request = JSON.parse(String(init?.body));
      return new Response(JSON.stringify({ ...provider, ...request.changes, revision: 3 }), { status: 200 });
    });
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const updated = await client.updateProvider(provider.id, {
      name: provider.name,
      providerType: provider.provider_type,
      endpoint: provider.endpoint,
      local: false,
      defaultModel: undefined,
      modelAllowlist: provider.model_allowlist,
      permitsSensitiveData: false,
      autoShareToolResults: false,
      residency: [],
      metadata: { default_model: "old-explicit" },
      expectedRevision: 2,
    });

    expect(updated.defaultModel).toBeUndefined();
    expect(updated.effectiveDefaultModel).toBe("allowed-first");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).changes.metadata).toEqual({});
    await expect(client.createProvider({ name: "Anthropic", providerType: "anthropic", local: false })).resolves.toBeDefined();
  });

  it("lists, ingests, reindexes, and deletes engagement knowledge sources", async () => {
    const source = {
      id: "knowledge-1",
      engagement_id: "engagement-1",
      name: "scope.md",
      source_type: "document",
      artifact_id: "artifact-1",
      status: "ready",
      citation: "scope.md",
      document_count: 3,
      metadata: {
        filename: "scope.md",
        media_type: "text/markdown",
        size: 42,
        sha256: "a".repeat(64),
        chunk_count: 3,
        indexed_at: "2026-07-12T12:00:00Z",
      },
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T12:00:00Z",
      revision: 1,
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify([source]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ backend: "chromadb", state: "downloading", model: "all-MiniLM-L6-v2", downloaded_bytes: 41_589_410, total_bytes: 83_178_821, detail: null }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(source), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...source, citation: "https://docs.example.com/scope", metadata: { ...source.metadata, origin: "url", source_url: "https://docs.example.com/scope" } }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...source, revision: 2 }), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const listed = await client.listKnowledgeSources("engagement-1");
    const indexStatus = await client.getKnowledgeIndexStatus();
    const ingested = await client.ingestKnowledgeSource({
      engagementId: "engagement-1",
      filename: "scope.md",
      mediaType: "text/markdown",
      contentBase64: "IyBTY29wZQ==",
    });
    const urlIngested = await client.ingestKnowledgeUrlSource({
      engagementId: "engagement-1",
      url: "https://docs.example.com/scope?token=secret",
    });
    await client.reindexKnowledgeSource("knowledge-1");
    await client.deleteKnowledgeSource("knowledge-1");

    expect(listed.items[0]).toMatchObject({
      artifactId: "artifact-1",
      documentCount: 3,
      metadata: { filename: "scope.md", mediaType: "text/markdown", chunkCount: 3 },
    });
    expect(ingested.engagementId).toBe("engagement-1");
    expect(urlIngested.metadata).toMatchObject({
      origin: "url",
      sourceUrl: "https://docs.example.com/scope",
    });
    expect(indexStatus).toEqual({
      backend: "chromadb",
      state: "downloading",
      model: "all-MiniLM-L6-v2",
      downloadedBytes: 41_589_410,
      totalBytes: 83_178_821,
      detail: undefined,
    });
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:8765/api/v1/knowledge?engagement_id=engagement-1&limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/knowledge/index-status",
      "http://127.0.0.1:8765/api/v1/knowledge/ingest",
      "http://127.0.0.1:8765/api/v1/knowledge/ingest-url",
      "http://127.0.0.1:8765/api/v1/knowledge/knowledge-1/reindex",
      "http://127.0.0.1:8765/api/v1/knowledge/knowledge-1",
    ]);
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toEqual({
      engagement_id: "engagement-1",
      filename: "scope.md",
      media_type: "text/markdown",
      content_base64: "IyBTY29wZQ==",
    });
    expect(JSON.parse(String(fetchMock.mock.calls[3][1]?.body))).toEqual({
      engagement_id: "engagement-1",
      url: "https://docs.example.com/scope?token=secret",
    });
  });

  it("renders the normalized chat SSE contract and sends durable privacy state", async () => {
    const encoder = new TextEncoder();
    const frames = [
      'event: started\ndata: {"type":"started","provider_id":"provider-1","model":"model-1","session_id":"session-1"}\n\n',
      'event: delta\ndata: {"type":"delta","provider_id":"provider-1","model":"model-1","delta":"hel"}\n\n',
      'event: delta\ndata: {"type":"delta","provider_id":"provider-1","model":"model-1","delta":"lo"}\n\n',
      'event: done\ndata: {"type":"done","session_id":"session-1","provider_id":"provider-1","model":"model-1","message":{"role":"assistant","content":"hello"},"usage":{"input_tokens":4,"output_tokens":1,"total_tokens":5},"finish_reason":"stop","provider_request_id":"request-1","citations":[{"source_id":"source-1","name":"scope.md","citation":"scope.md","artifact_id":"artifact-1","chunk_id":"chunk-1","page":2,"excerpt":"Approved scope"}]}\n\n',
    ];
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        frames.forEach((frame) => controller.enqueue(encoder.encode(frame)));
        controller.close();
      },
    });
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(stream, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", token: "token", fetch: fetchMock });
    const events: string[] = [];

    const result = await client.streamChat({
      providerId: "provider-1",
      engagementId: "engagement-1",
      sessionId: "session-1",
      model: "model-1",
      messages: [{ role: "user", content: "hello" }],
      includeKnowledge: true,
      allowCloudKnowledge: true,
    }, (event) => events.push(event.type));

    expect(events).toEqual(["started", "delta", "delta", "done"]);
    expect(result).toMatchObject({
      sessionId: "session-1",
      message: { role: "assistant", content: "hello" },
      usage: { totalTokens: 5 },
      citations: [{ sourceId: "source-1", chunkId: "chunk-1", page: 2 }],
    });
    const request = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(request).toMatchObject({
      provider_id: "provider-1",
      engagement_id: "engagement-1",
      session_id: "session-1",
      model: "model-1",
      stream: true,
      include_knowledge: true,
      allow_cloud_knowledge: true,
      max_artifact_queries: null,
    });
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("Authorization")).toBe("Bearer token");
  });

  it("maps harness lifecycle status and independent-session rollover frames", async () => {
    const encoder = new TextEncoder();
    const frames = [
      'event: status\ndata: {"type":"status","harness_session_id":"session-new","harness_turn_id":"turn-1","payload":{"phase":"parallel_session_created","detail":"Started an independent harness session for parallel work.","previous_session_id":"session-old"}}\n\n',
      'event: status\ndata: {"type":"status","harness_session_id":"session-new","harness_turn_id":"turn-1","payload":{"phase":"connecting","detail":"Connecting to the harness runtime."}}\n\n',
      'event: done\ndata: {"type":"done","session_id":"chat-1","harness_profile_id":"harness-1","harness_session_id":"session-new","harness_turn_id":"turn-1","model":"model-1","message":{"role":"assistant","content":"complete"},"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2},"finish_reason":"stop","citations":[]}\n\n',
    ];
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        frames.forEach((frame) => controller.enqueue(encoder.encode(frame)));
        controller.close();
      },
    });
    document.cookie = "nebula_csrf=paired-csrf-token; path=/";
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(stream, { status: 200 }));
    const client = new ApiClient({
      baseUrl: "http://127.0.0.1:8765",
      fetch: fetchMock,
    });
    const events: Array<{ type: string; phase?: string; harnessSessionId?: string; previousSessionId?: string }> = [];

    const result = await client.streamChat({
      harnessProfileId: "harness-1",
      harnessSessionId: "session-old",
      engagementId: "engagement-1",
      model: "model-1",
      harnessReasoningEffort: "high",
      harnessServiceTier: "fast",
      messages: [{ role: "user", content: "work in parallel" }],
    }, (event) => events.push(event));

    expect(events).toMatchObject([
      { type: "status", phase: "parallel_session_created", harnessSessionId: "session-new", previousSessionId: "session-old" },
      { type: "status", phase: "connecting", harnessSessionId: "session-new" },
      { type: "done", harnessSessionId: "session-new" },
    ]);
    expect(result).toMatchObject({
      harnessSessionId: "session-new",
      harnessTurnId: "turn-1",
      message: { content: "complete" },
    });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
      harness_reasoning_effort: "high",
      harness_service_tier: "fast",
    });
    const headers = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(headers.get("Authorization")).toBeNull();
    expect(headers.get("X-Nebula-CSRF")).toBe("paired-csrf-token");
    document.cookie = "nebula_csrf=; Max-Age=0; path=/";
  });

  it("loads durable chat session summaries and ordered messages", async () => {
    const entity = { created_at: "2026-07-12T11:00:00Z", updated_at: "2026-07-12T12:00:00Z", revision: 1 };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        ...entity,
        id: "session-1",
        engagement_id: "engagement-1",
        title: "Review scope",
        provider_profile_id: "provider-1",
        model: "model-1",
        metadata: {},
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        ...entity,
        id: "message-1",
        engagement_id: "engagement-1",
        session_id: "session-1",
        sequence: 1,
        role: "user",
        content: "Review scope",
        citations: [],
        metadata: { tool_results: [{
          tool_call_id: "tool-1",
          capability: "Search evidence",
          status: "complete",
          summary: "Found evidence.",
          evidence_ids: ["evidence-1"],
          result_artifact_id: "artifact-1",
        }] },
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ...entity,
        revision: 2,
        id: "session-1",
        engagement_id: "engagement-1",
        title: "Renamed scope review",
        provider_profile_id: "provider-1",
        model: "model-1",
        metadata: {},
      }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const sessions = await client.listChatSessions("engagement-1");
    const messages = await client.listChatMessages("session-1");
    const renamed = await client.renameChatSession("session-1", { title: "  Renamed scope review  ", expectedRevision: 1 });

    expect(sessions.items[0]).toMatchObject({ id: "session-1", providerId: "provider-1", revision: 1 });
    expect(messages[0]).toMatchObject({ sessionId: "session-1", sequence: 1, role: "user" });
    expect(messages[0].toolResults).toEqual([expect.objectContaining({
      toolCallId: "tool-1",
      capability: "Search evidence",
      status: "complete",
      summary: "Found evidence.",
      evidenceIds: ["evidence-1"],
      resultArtifactId: "artifact-1",
    })]);
    expect(renamed).toMatchObject({ id: "session-1", title: "Renamed scope review", revision: 2 });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/chat-sessions?engagement_id=engagement-1&limit=1000&offset=0",
    );
    expect(fetchMock.mock.calls[2][0]).toBe("http://127.0.0.1:8765/api/v1/chat-sessions/session-1");
    expect(fetchMock.mock.calls[2][1]).toMatchObject({
      method: "PATCH",
      body: JSON.stringify({ title: "Renamed scope review", expected_revision: 1 }),
    });
  });

  it("archives and restores chat sessions through the session patch", async () => {
    const wire = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 3, id: "session-1", engagement_id: "engagement-1", title: "Scope review", provider_profile_id: "provider-1", model: "model-1" };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...wire, metadata: { archived_at: "2026-07-12T11:00:00Z" } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...wire, revision: 4, metadata: {} }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    expect(await client.setChatSessionArchived("session-1", true, 2)).toMatchObject({ archivedAt: "2026-07-12T11:00:00Z" });
    expect((await client.setChatSessionArchived("session-1", false)).archivedAt).toBeUndefined();
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "PATCH", body: JSON.stringify({ archived: true, expected_revision: 2 }) });
  });

  it("maps and updates durable assistant selections on chat sessions", async () => {
    const wire = {
      created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z",
      revision: 4, id: "session-1", engagement_id: "engagement-1", title: "Scope review",
      provider_profile_id: "provider-1", model: "model-1",
      metadata: { mcp_server_ids: ["mcp-1"], hook_ids: ["audit"], reasoning_effort: "high" },
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify(wire), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.updateChatSessionAssistantSettings("session-1", {
      mcpServerIds: ["mcp-1"], hookIds: ["audit"], reasoningEffort: "high", expectedRevision: 3,
    })).resolves.toMatchObject({ mcpServerIds: ["mcp-1"], hookIds: ["audit"], reasoningEffort: "high", revision: 4 });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "PATCH",
      body: JSON.stringify({ mcp_server_ids: ["mcp-1"], hook_ids: ["audit"], reasoning_effort: "high", expected_revision: 3 }),
    });
  });

  it("saves the reasoning effort alone so it can change while a response runs", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => new Response(JSON.stringify({
      created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z",
      revision: 5, id: "session-1", engagement_id: "engagement-1", title: "Scope review",
      provider_profile_id: "provider-1", model: "model-1", metadata: { reasoning_effort: "low" },
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.updateChatSessionAssistantSettings("session-1", { reasoningEffort: "low" }))
      .resolves.toMatchObject({ reasoningEffort: "low", revision: 5 });
    await client.updateChatSessionAssistantSettings("session-1", { useModelReasoningDefault: true });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ reasoning_effort: "low" });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ reasoning_effort: null });
  });

  it("maps provenance-backed chat and mission context status", async () => {
    const status = {
      owner_type: "chat_session",
      owner_id: "session-1",
      status: "ready",
      context_window: 8192,
      max_output_tokens: 2048,
      target_input_tokens: 4608,
      compacted_input_target: 3686,
      capacity_source: "model_catalog",
      capacity_estimated: false,
      metadata_revision: "catalog-sha",
      route_limits_verified: true,
      eligible_route_count: 2,
      route_context_window: 65536,
      route_input_limit: 60000,
      route_limits_required: true,
      estimated_input_tokens: 5000,
      compacted_through: 42,
      source_references: [{ source_kind: "chat_message", source_id: "message-1", sequence: 1 }],
      compaction_usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
      compaction_cost_usd: 0.01,
      snapshot: {
        id: "snapshot-1",
        created_at: "2026-07-12T11:00:00Z",
        updated_at: "2026-07-12T11:00:00Z",
        revision: 1,
        owner_type: "chat_session",
        owner_id: "session-1",
        version: 1,
        status: "ready",
        compacted_through: 42,
        memory: {
          summary: "Earlier context retained.",
          confirmed_facts: [{
            text: "Port 8443 was selected.",
            sources: [{ source_kind: "chat_message", source_id: "message-1", sequence: 1 }],
          }],
        },
        source_references: [{ source_kind: "chat_message", source_id: "message-1", sequence: 1 }],
        provider_profile_id: "provider-1",
        model: "model-1",
        prompt_version: "v1",
        source_sha256: "a".repeat(64),
        usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
        cost_usd: 0.01,
      },
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify(status), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ...status,
        owner_type: "agent_run",
        owner_id: "run-1",
        snapshot: { ...status.snapshot, owner_type: "agent_run", owner_id: "run-1" },
      }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const chat = await client.getChatContext("session-1");
    const mission = await client.getRunContext("run-1");

    expect(chat).toMatchObject({
      status: "ready",
      contextWindow: 8192,
      compactedInputTarget: 3686,
      capacitySource: "model_catalog",
      capacityEstimated: false,
      metadataRevision: "catalog-sha",
      routeLimitsVerified: true,
      eligibleRouteCount: 2,
      routeContextWindow: 65536,
      routeInputLimit: 60000,
      routeLimitsRequired: true,
      compactedThrough: 42,
      compactionUsage: { totalTokens: 15 },
      compactionCostUsd: 0.01,
      snapshot: {
        providerId: "provider-1",
        memory: { summary: "Earlier context retained." },
        usage: { totalTokens: 15 },
      },
    });
    expect(chat.snapshot?.memory?.confirmedFacts[0].sources[0]).toEqual({
      sourceKind: "chat_message",
      sourceId: "message-1",
      sequence: 1,
    });
    // An older Core sends none of the v2 memory, quality, calibration or notes
    // fields; the status still maps, with nothing claimed on its behalf.
    expect(chat.snapshot?.memory).toMatchObject({ userRequests: [], currentState: [], attempts: [], references: [] });
    expect(chat.snapshot).toMatchObject({ quality: "complete", droppedItems: 0 });
    expect(chat.quality).toBeUndefined();
    expect(chat.estimateCalibration).toBeUndefined();
    expect(chat.workingNotes).toBeUndefined();
    expect(chat.bindingLimit).toBeUndefined();
    expect(chat.inputCapacity).toBeUndefined();
    expect(chat.inputLimitBinds).toBe(false);
    expect(mission.ownerType).toBe("agent_run");
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:8765/api/v1/chat/sessions/session-1/context",
      "http://127.0.0.1:8765/api/v1/runs/run-1/context",
    ]);
  });

  it("maps v2 context memory, snapshot quality, calibration, cache hits and working notes", async () => {
    const reference = { source_kind: "chat_message", source_id: "message-1", sequence: 1 };
    const cited = (text: string) => ({ text, sources: [reference] });
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      owner_type: "chat_session",
      owner_id: "session-1",
      status: "ready",
      context_window: 128000,
      max_output_tokens: 8000,
      target_input_tokens: 91500,
      estimated_input_tokens: 71240,
      estimate_calibration: 1.12,
      binding_limit: "configured",
      input_capacity: 14000,
      input_limit_binds: false,
      quality: "degraded",
      last_provider_request: {
        instructions: 4000, conversation: 60000, tool_schemas: 5000, tool_results: 0, other: 0,
        estimated_total: 69000, reported_input_tokens: 68912, reported_cached_input_tokens: 42036, attempt: 1,
      },
      compacted_through: 42,
      working_notes: { content: "## Todo\n- [ ] Ask about .40", revision: 6, updated_at: "2026-09-26T10:00:00Z", turn_id: "turn-9" },
      snapshot: {
        id: "snapshot-1", created_at: "2026-09-26T10:00:00Z", updated_at: "2026-09-26T10:00:00Z", revision: 1,
        owner_type: "chat_session", owner_id: "session-1", version: 2, status: "ready", compacted_through: 42,
        quality: "salvaged", dropped_items: 3,
        memory: {
          summary: "Reviewing TLS.",
          user_requests: [cited("Map 10.20.0.0/24.")],
          current_state: [cited("12 of 17 listeners checked.")],
          attempts: [cited("sslyze timed out.")],
          references: [cited("/home/op/scans/tls.json")],
        },
        source_references: [reference],
        provider_profile_id: "provider-1", model: "model-1", prompt_version: "nebula-context-v2",
      },
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const context = await client.getChatContext("session-1");

    expect(context.estimateCalibration).toBe(1.12);
    expect(context).toMatchObject({ bindingLimit: "configured", inputCapacity: 14000, inputLimitBinds: false });
    // Core's status names the served snapshot's quality; it wins over the row.
    expect(context.quality).toBe("degraded");
    expect(context.snapshot).toMatchObject({ quality: "salvaged", droppedItems: 3 });
    expect(context.lastProviderRequest).toMatchObject({ reportedInputTokens: 68912, reportedCachedInputTokens: 42036 });
    expect(context.workingNotes).toEqual({ content: "## Todo\n- [ ] Ask about .40", revision: 6, updatedAt: "2026-09-26T10:00:00Z", turnId: "turn-9" });
    expect(context.snapshot?.memory).toMatchObject({
      userRequests: [{ text: "Map 10.20.0.0/24.", sources: [{ sourceKind: "chat_message", sourceId: "message-1", sequence: 1 }] }],
      currentState: [{ text: "12 of 17 listeners checked." }],
      attempts: [{ text: "sslyze timed out." }],
      references: [{ text: "/home/op/scans/tls.json" }],
      decisions: [],
      corrections: [],
    });
  });

  it("falls back to the snapshot's quality and ignores unknown quality or empty notes", async () => {
    const base = {
      owner_type: "chat_session", owner_id: "session-1", status: "ready", context_window: 8192,
      max_output_tokens: 2048, target_input_tokens: 4608,
      snapshot: {
        id: "snapshot-1", created_at: "2026-09-26T10:00:00Z", updated_at: "2026-09-26T10:00:00Z", revision: 1,
        owner_type: "chat_session", owner_id: "session-1", version: 1, status: "ready", compacted_through: 4,
        quality: "degraded", memory: { summary: "Extract." }, provider_profile_id: "p", model: "m", prompt_version: "v2",
      },
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...base, working_notes: { content: "  ", revision: 1, updated_at: "2026-09-26T10:00:00Z" } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...base, quality: "experimental", snapshot: { ...base.snapshot, quality: "experimental" }, estimate_calibration: null, working_notes: null, binding_limit: "experimental" }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const first = await client.getChatContext("session-1");
    expect(first.quality).toBe("degraded");
    expect(first.workingNotes).toBeUndefined();

    const second = await client.getChatContext("session-1");
    expect(second.quality).toBeUndefined();
    expect(second.snapshot?.quality).toBe("complete");
    expect(second.estimateCalibration).toBeUndefined();
    expect(second.bindingLimit).toBeUndefined();
    expect(second.workingNotes).toBeUndefined();
  });

  it("maps generic engagement, asset, report, evidence, and mission mutations", async () => {
    const entity = { created_at: "2026-07-12T11:00:00Z", updated_at: "2026-07-12T12:00:00Z", revision: 1 };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/engagements")) return new Response(JSON.stringify({ ...entity, id: "engagement-1", name: "Client review", description: "Bounded review", client_name: "Client", status: "draft", tags: ["external"], metadata: {} }), { status: 201 });
      if (path.endsWith("/assets")) return new Response(JSON.stringify({ ...entity, id: "asset-1", engagement_id: "engagement-1", asset_type: "domain", name: "api.example.test", hostname: "api.example.test", criticality: "high", exposed: true, tags: ["api"], metadata: {} }), { status: 201 });
      if (path.endsWith("/reports/report-1")) return new Response(JSON.stringify({ ...entity, revision: 2, id: "report-1", engagement_id: "engagement-1", title: "Assessment", status: "review", executive_summary: "Updated", finding_ids: ["finding-1"], artifact_ids: [], metadata: {} }), { status: 200 });
      if (path.endsWith("/reports")) return new Response(JSON.stringify({ ...entity, id: "report-1", engagement_id: "engagement-1", title: "Assessment", status: "draft", executive_summary: "", finding_ids: [], artifact_ids: [], metadata: {} }), { status: 201 });
      if (path.endsWith("/evidence/upload")) return new Response(JSON.stringify({ ...entity, id: "evidence-1", engagement_id: "engagement-1", evidence_type: "operator_upload", title: "proof.txt", description: "Proof", artifact_id: "artifact-1", finding_id: null, asset_ids: ["asset-1"], sha256: "a".repeat(64), captured_at: entity.created_at, captured_by: "operator", source_version: null, metadata: { filename: "proof.txt", media_type: "text/plain", size: 5, source: "operator_upload" } }), { status: 201 });
      if (path.endsWith("/missions")) return new Response(JSON.stringify({ ...entity, id: "run-1", engagement_id: "engagement-1", objective: "Review scope", status: "queued", started_at: null, completed_at: null, metadata: {} }), { status: 202 });
      if (path.endsWith("/runs/run-1/stop")) return new Response(JSON.stringify({ ...entity, id: "run-1", engagement_id: "engagement-1", objective: "Review scope", status: "cancelled", started_at: null, completed_at: entity.updated_at, metadata: {} }), { status: 200 });
      return new Response(JSON.stringify({ detail: `${init?.method} ${path} not mocked` }), { status: 500 });
    });
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const engagement = await client.createEngagement({ name: "Client review", description: "Bounded review", clientName: "Client", tags: ["external"] });
    const asset = await client.createAsset({ engagementId: engagement.id, name: "  api.example.test  ", kind: "domain", hostname: "api.example.test", criticality: "high", exposure: "external", tags: ["api"] });
    const report = await client.createReport({ engagementId: engagement.id, title: "  Assessment  " });
    const updatedReport = await client.updateReport(report.id, { status: "review", executiveSummary: "Updated", findingIds: ["finding-1"], expectedRevision: 1 });
    const evidence = await client.uploadEvidence({ engagementId: engagement.id, filename: "proof.txt", title: "proof.txt", evidenceType: "operator_upload", contentBase64: "cHJvb2Y=", mediaType: "text/plain", description: "Proof", assetIds: [asset.id] });
    const run = await client.createMission({ engagementId: engagement.id, name: "Scope review", objective: "Review scope", providerId: "provider-1", model: "model-1", maxDurationSeconds: 600, maxTokens: 2000, maxCostUsd: 2, maxRetries: 1 });
    const stopped = await client.stopRun(run.id, { reason: "Operator requested" });

    expect(engagement).toMatchObject({ description: "Bounded review", clientName: "Client", tags: ["external"] });
    expect(asset).toMatchObject({ hostname: "api.example.test", criticality: "high", exposure: "external", tags: ["api"] });
    expect(updatedReport).toMatchObject({ revision: 2, status: "review", executiveSummary: "Updated" });
    expect(evidence).toMatchObject({ artifactId: "artifact-1", metadata: { filename: "proof.txt", mediaType: "text/plain", size: 5 } });
    expect(stopped.status).toBe("cancelled");
    expect(JSON.parse(String(fetchMock.mock.calls.find(([input]) => String(input).endsWith("/api/v1/assets"))?.[1]?.body)).name).toBe("api.example.test");
    expect(JSON.parse(String(fetchMock.mock.calls.find(([input]) => String(input).endsWith("/api/v1/reports"))?.[1]?.body)).title).toBe("Assessment");
    expect(JSON.parse(String(fetchMock.mock.calls.find(([input]) => String(input).endsWith("/api/v1/missions"))?.[1]?.body))).toMatchObject({ provider_id: "provider-1", model: "model-1", max_duration_seconds: 600 });
  });

  it("omits mission resource limits by default and preserves explicit budgets", async () => {
    const response = { id: "run-1", engagement_id: "engagement-1", objective: "Review", status: "queued", created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T10:00:00Z", revision: 1, metadata: {} };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => new Response(JSON.stringify(response), { status: 202 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await client.createMission({ engagementId: "engagement-1", name: "Review", objective: "Review", providerId: "provider-1", model: "model-1" });
    await client.createMission({ engagementId: "engagement-1", name: "Scan", objective: "Scan", providerId: "provider-1", model: "model-1", maxToolCalls: 20, maxConcurrency: 2 });

    const defaultBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(defaultBody).toMatchObject({ max_concurrency: 1 });
    expect(defaultBody).not.toHaveProperty("max_duration_seconds");
    expect(defaultBody).not.toHaveProperty("max_tokens");
    expect(defaultBody).not.toHaveProperty("max_cost_usd");
    expect(defaultBody).not.toHaveProperty("max_tool_calls");
    expect(defaultBody).not.toHaveProperty("max_artifact_queries");
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({ max_tool_calls: 20, max_concurrency: 2 });
  });

  it("maps harness registries and sends explicit harness mission privacy consent", async () => {
    const entity = { created_at: "2026-07-14T10:00:00Z", updated_at: "2026-07-14T10:00:00Z", revision: 1 };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/harnesses")) return new Response(JSON.stringify([{
        ...entity,
        id: "harness-1",
        name: "Codex",
        kind: "codex_app_server",
        connection_mode: "spawn",
        transport: "stdio",
        executable: "/opt/codex",
        auth_mode: "existing_session",
        default_model: "gpt-test",
        enabled: true,
        privacy: { local_only: false, permits_sensitive_data: true },
        native_capabilities: {
          workspace_access: "read",
          shell: true,
          web_search: true,
          subagents: true,
        },
        capabilities: {
          checked_at: entity.updated_at,
          harness_version: "0.144.0",
          models: ["gpt-test", "gpt-next"],
          model_options: [{
            model: "gpt-test",
            reasoning_efforts: [{ id: "high", label: "High", description: "Quality first" }],
            default_reasoning_effort: "high",
            service_tiers: [{ id: "fast", label: "Fast", description: "Priority processing" }],
            default_service_tier: "fast",
          }],
        },
      }]), { status: 200 });
      if (path.endsWith("/mcp-servers")) return new Response(JSON.stringify([{
        ...entity,
        id: "mcp-1",
        name: "workspace",
        description: "Shared project files.",
        transport: "streamable_http",
        url: "https://mcp.example.test/mcp",
        auth_mode: "none",
        enabled: true,
        required: true,
        trusted_stdio: false,
        default_approval: "risk_based",
        capabilities: { tools: [{ name: "read_file", description: "Read", read_only: true, destructive: false, open_world: false, credentialed: false }] },
      }]), { status: 200 });
      if (path.endsWith("/harness-sessions")) return new Response(JSON.stringify([{
        ...entity,
        id: "session-1",
        engagement_id: "engagement-1",
        harness_profile_id: "harness-1",
        model: "gpt-test",
        status: "idle",
        mcp_server_ids: ["mcp-1"],
        metadata: { runtime_options: { reasoning_effort: "high", service_tier: "fast" } },
        last_activity_at: entity.updated_at,
      }]), { status: 200 });
      if (path.endsWith("/harness-sessions/session-1/activity")) return new Response(JSON.stringify({
        session_id: "session-1",
        session_status: "running",
        busy: true,
        live: true,
        turn_id: "turn-1",
        turn_status: "running",
        turn_origin: "chat",
        started_at: entity.updated_at,
        last_activity_at: entity.updated_at,
        detail: "A harness turn is currently running.",
      }), { status: 200 });
      if (path.endsWith("/missions")) return new Response(JSON.stringify({
        ...entity,
        id: "run-harness",
        engagement_id: "engagement-1",
        objective: "Inspect",
        status: "queued",
        backend: "harness",
        harness_profile_id: "harness-1",
        harness_session_id: "session-1",
        metadata: {},
      }), { status: 202 });
      return new Response(JSON.stringify({ detail: "not mocked" }), { status: 500 });
    });
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const [harness] = await client.listHarnesses();
    const [server] = await client.listMcpServers();
    const [session] = await client.listHarnessSessions("engagement-1");
    const activity = await client.getHarnessSessionActivity(session.id);
    const run = await client.createMission({
      engagementId: "engagement-1",
      name: "Harness inspection",
      objective: "Inspect",
      backend: "harness",
      harnessProfileId: harness.id,
      harnessSessionId: session.id,
      model: "gpt-test",
      maxToolCalls: 20,
      allowCloudToolResults: true,
    });

    expect(harness).toMatchObject({
      version: "0.144.0",
      models: ["gpt-test", "gpt-next"],
      modelOptions: [{
        model: "gpt-test",
        defaultReasoningEffort: "high",
        reasoningEfforts: [{ id: "high", label: "High" }],
        defaultServiceTier: "fast",
        serviceTiers: [{ id: "fast", label: "Fast" }],
      }],
      localOnly: false,
      permitsSensitiveData: true,
      nativeCapabilities: {
        workspaceAccess: "read",
        shell: true,
        webSearch: true,
        subagents: true,
      },
    });
    expect(server).toMatchObject({ description: "Shared project files.", required: true, tools: [{ name: "read_file", readOnly: true }] });
    expect(session).toMatchObject({ harnessProfileId: "harness-1", mcpServerIds: ["mcp-1"], reasoningEffort: "high", serviceTier: "fast" });
    expect(activity).toMatchObject({ sessionId: "session-1", busy: true, live: true, turnId: "turn-1", turnStatus: "running" });
    expect(run).toMatchObject({ backend: "harness", harnessSessionId: "session-1" });
    const missionBody = JSON.parse(String(fetchMock.mock.calls.find(([input]) => String(input).endsWith("/missions"))?.[1]?.body));
    expect(missionBody).toMatchObject({
      backend: "harness",
      harness_profile_id: "harness-1",
      harness_session_id: "session-1",
      allow_cloud_tool_results: true,
    });
    expect(missionBody).not.toHaveProperty("provider_id");
  });

  it("includes Grok ACP while omitting legacy Claude profiles from the provided harnesses", async () => {
    const entity = { created_at: "2026-07-14T10:00:00Z", updated_at: "2026-07-14T10:00:00Z", revision: 1 };
    const profile = {
      ...entity,
      connection_mode: "spawn",
      transport: "stdio",
      auth_mode: "existing_session",
      enabled: true,
      privacy: { local_only: false, permits_sensitive_data: false },
      native_capabilities: { workspace_access: "none" },
    };
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify([
      { ...profile, id: "legacy-claude", name: "Legacy Claude", kind: "claude_agent_sdk", capabilities: { checked_at: entity.updated_at, models: ["sonnet"] } },
      { ...profile, id: "grok-1", name: "Grok", kind: "grok_acp", capabilities: { checked_at: entity.updated_at, models: ["grok-build"] } },
    ]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.listHarnesses()).resolves.toMatchObject([{ id: "grok-1", kind: "grok_acp", models: ["grok-build"] }]);
  });

  it("assembles every paginated terminal result byte and acknowledges raw access", async () => {
    const pages = [new Uint8Array([0, 1, 2]), new Uint8Array([3, 4])];
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(pages[0], {
        headers: { "X-Nebula-Output-Total": "5", "X-Nebula-Output-Next": "3" },
      }))
      .mockResolvedValueOnce(new Response(pages[1], {
        headers: { "X-Nebula-Output-Total": "5", "X-Nebula-Output-Next": "5" },
      }));
    const client = new ApiClient({
      baseUrl: "http://127.0.0.1:8765",
      token: "terminal-token",
      fetch: fetchMock,
    });

    const result = await client.terminalCommandOutput("project/one", "command/one", true);

    expect(Array.from(new Uint8Array(await result.arrayBuffer()))).toEqual([0, 1, 2, 3, 4]);
    expect(String(fetchMock.mock.calls[0][0])).toContain("offset=0");
    expect(String(fetchMock.mock.calls[1][0])).toContain("offset=3");
    for (const call of fetchMock.mock.calls) {
      const headers = new Headers(call[1]?.headers);
      expect(headers.get("X-Nebula-Sensitive-Data-Acknowledged")).toBe("true");
      expect(headers.get("Authorization")).toBe("Bearer terminal-token");
    }
  });

  it("maps durable harness activity cursors and detailed usage", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      events: [{
        schema_version: "nebula.harness-activity/v1",
        id: "event-7",
        sequence: 7,
        type: "item_upsert",
        vendor: "claude_agent_sdk",
        harness_session_id: "session-1",
        harness_turn_id: "turn-1",
        item_id: "command-1",
        parent_item_id: "agent-1",
        item_kind: "command",
        item_status: "completed",
        title: "Run tests",
        artifact_ids: ["artifact-1"],
        payload: { cwd: "/workspace", exit_code: 0 },
        detailed_usage: {
          input_tokens: 12,
          output_tokens: 8,
          total_tokens: 20,
          cache_creation_input_tokens: 3,
          cache_read_input_tokens: 4,
          reasoning_output_tokens: 2,
          cost_usd: 0.01,
          duration_ms: 250,
          duration_api_ms: 200,
          num_turns: 1,
          context_used: 12,
          context_window: 200000,
        },
      }],
      next_sequence: 7,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const page = await client.getHarnessTurnEvents("turn/one", 3);

    expect(page.nextSequence).toBe(7);
    expect(page.events[0]).toMatchObject({
      itemId: "command-1",
      parentItemId: "agent-1",
      itemKind: "command",
      itemStatus: "completed",
      artifactIds: ["artifact-1"],
      detailedUsage: {
        totalTokens: 20,
        cacheCreationTokens: 3,
        cacheReadTokens: 4,
        reasoningTokens: 2,
        apiDurationMs: 200,
        contextLimitTokens: 200000,
      },
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/harness-turns/turn%2Fone/events?after=3&limit=10000",
    );
  });

  it("keeps the MCP server a replayed harness tool call reached", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      events: [{
        schema_version: "nebula.harness-activity/v2",
        id: "event-8",
        sequence: 8,
        type: "tool_started",
        vendor: "claude_agent_sdk",
        harness_turn_id: "turn-1",
        item_id: "tool-1",
        item_kind: "tool",
        item_status: "running",
        title: "create_issue",
        server_id: "tracker",
        artifact_ids: [],
        payload: {},
      }],
      next_sequence: 8,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const page = await client.getHarnessTurnEvents("turn-1");

    expect(page.events[0]).toMatchObject({ title: "create_issue", serverId: "tracker" });
  });

  it("maps bounded workspace search results and encodes the project path", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      engagement_id: "project/one",
      query: "scan target",
      mode: "text",
      matches: [{
        path: "src/scanner.py",
        kind: "content",
        line: 7,
        column: 3,
        preview: "scan_target(host)",
      }],
      scanned_files: 12,
      truncated: false,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.searchWorkspace("project/one", "scan target", "text", "src tools")).resolves.toEqual({
      engagementId: "project/one",
      query: "scan target",
      mode: "text",
      matches: [{ path: "src/scanner.py", kind: "content", line: 7, column: 3, preview: "scan_target(host)" }],
      scannedFiles: 12,
      truncated: false,
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/engagements/project%2Fone/workspace/search?query=scan+target&mode=text&path=src+tools&limit=100",
    );
  });

  it("requests and maps a later host-folder page", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      path: "/projects",
      parent: "/",
      directories: [{ name: "project-500", path: "/projects/project-500" }],
      truncated: false,
      next_offset: null,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.listHostWorkspaceFolders("/projects", 500)).resolves.toEqual({
      path: "/projects",
      parent: "/",
      directories: [{ name: "project-500", path: "/projects/project-500" }],
      truncated: false,
      nextOffset: undefined,
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/workspace-folders?path=%2Fprojects&offset=500",
    );
  });

  it("requests a host-folder name filter", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      path: "/projects",
      parent: "/",
      directories: [{ name: "Research", path: "/projects/Research" }],
      truncated: false,
      next_offset: null,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await client.listHostWorkspaceFolders("/projects", 0, " Research ");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/workspace-folders?path=%2Fprojects&filter=Research",
    );
  });

  it("maps bounded VS Code launch profiles and preserves disabled reasons", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      engagement_id: "project/one",
      active_path: "research/parser.py",
      configurations: [{
        id: "profile-1",
        name: "Debug parser",
        path: "research/parser.py",
        arguments: ["--fixture", "/workspace/sample.bin"],
        source: ".vscode/launch.json",
        detail: "VS Code Python launch profile",
        supported: false,
        unsupported_reason: "Save the profile before use.",
      }],
      truncated: false,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.workspaceDebugConfigurations("project/one", "research/parser.py")).resolves.toEqual({
      engagementId: "project/one",
      activePath: "research/parser.py",
      configurations: [{
        id: "profile-1",
        name: "Debug parser",
        path: "research/parser.py",
        arguments: ["--fixture", "/workspace/sample.bin"],
        source: ".vscode/launch.json",
        detail: "VS Code Python launch profile",
        supported: false,
        unsupportedReason: "Save the profile before use.",
      }],
      truncated: false,
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/engagements/project%2Fone/workspace/debug-configurations?path=research%2Fparser.py",
    );
  });

  it("maps source-control status and requests hardened diffs", async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        engagement_id: "project/one",
        state: "ready",
        branch: "research/fix",
        head: "abcdef123456",
        files: [{
          path: "src/scanner.py",
          index_status: "unmodified",
          worktree_status: "modified",
          original_path: null,
        }],
        truncated: false,
        detail: "1 changed path.",
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        engagement_id: "project/one",
        path: "src/scanner.py",
        staged: false,
        text: "@@ -1 +1 @@",
        truncated: false,
        head: "abcdef123456",
      }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await expect(client.sourceControlStatus("project/one")).resolves.toMatchObject({
      engagementId: "project/one",
      state: "ready",
      branch: "research/fix",
      files: [{ path: "src/scanner.py", worktreeStatus: "modified" }],
    });
    await expect(client.sourceControlDiff("project/one", "src/scanner.py")).resolves.toMatchObject({
      path: "src/scanner.py",
      text: "@@ -1 +1 @@",
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8765/api/v1/engagements/project%2Fone/workspace/source-control/diff?path=src%2Fscanner.py&staged=false",
    );
  });

  it("serializes a bounded issue-validation grant and maps its authority receipt", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "grant-1",
      revision: 1,
      assessment_id: "assessment-1",
      candidate_id: "candidate/one",
      target_url: "https://app.example.test/search",
      technique: "Replay one inert marker and one negative encoding control.",
      max_requests: 8,
      requests_used: 0,
      duration_seconds: 300,
      expires_at: "2026-08-28T12:05:00Z",
      status: "active",
      created_at: "2026-08-28T12:00:00Z",
      updated_at: "2026-08-28T12:00:00Z",
    }), { status: 201, headers: { "content-type": "application/json" } }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const grant = await client.grantSecurityBrowserCandidateValidation({
      id: "candidate/one", revision: 4, assessmentId: "assessment-1", ruleId: "reflected-input",
      checkFamily: "xss", title: "Reflected input", targetUrl: "https://app.example.test/search",
      severity: "medium", confidence: "firm", evidenceIds: ["evidence-1"], validationStatus: "unvalidated",
    }, {
      technique: "Replay one inert marker and one negative encoding control.",
      maxRequests: 8,
      durationSeconds: 300,
    });

    expect(grant).toMatchObject({ id: "grant-1", maxRequests: 8, durationSeconds: 300, status: "active" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/api/v1/browser-issue-candidates/candidate%2Fone/validation-grant");
    expect(JSON.parse(String(init?.body))).toEqual({
      expected_candidate_revision: 4,
      technique: "Replay one inert marker and one negative encoding control.",
      max_requests: 8,
      duration_seconds: 300,
      idempotency_key: "validation-grant:candidate/one:4",
    });
  });
});

describe("project scope tool pinning", () => {
  it("maps always-loaded tools in both directions and lists the choices", async () => {
    const scope = {
      id: "scope:project", engagement_id: "project", allowed_cidrs: [], allowed_domains: [],
      allowed_urls: [], allowed_ports: [], allow_all_targets: false, prohibited_actions: [],
      local_only: true, tool_suggestions: false, always_loaded_tools: ["mcp.abc123abc123.search"],
      max_concurrency: 1, grants: [], revision: 3,
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify(scope), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        name: "mcp.abc123abc123.search", server_id: "mcp-1", server_name: "tracker",
        tool_name: "search", description: "Search issues.",
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(scope), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const loaded = await client.getEngagementScope("project");
    expect(loaded.alwaysLoadedTools).toEqual(["mcp.abc123abc123.search"]);
    // Older Core omits the field; on-demand loading is its default.
    expect(loaded.onDemandTools).toBe(true);
    await expect(client.listScopeToolCandidates("project")).resolves.toEqual([{
      name: "mcp.abc123abc123.search", serverId: "mcp-1", serverName: "tracker",
      toolName: "search", description: "Search issues.",
    }]);
    expect(String(fetchMock.mock.calls[1][0])).toBe("http://127.0.0.1:8765/api/v1/engagements/project/scope/tool-candidates");

    await client.updateEngagementScope("project", { ...loaded, alwaysLoadedTools: [], expectedRevision: 3 });
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toMatchObject({ always_loaded_tools: [] });
  });

  it("reads a project's on-demand loading switch without ever writing it back", async () => {
    const scope = { id: "scope:project", engagement_id: "project", on_demand_tools: false, revision: 4 };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify(scope), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(scope), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const loaded = await client.getEngagementScope("project");
    expect(loaded.onDemandTools).toBe(false);
    await client.updateEngagementScope("project", { ...loaded, expectedRevision: 4 });
    // Core keeps a field an update omits, so saving other settings never flips it.
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).not.toHaveProperty("on_demand_tools");
  });
});

describe("chat transport and list paging", () => {
  const request = {
    providerId: "provider-1",
    engagementId: "engagement-1",
    model: "model-1",
    messages: [{ role: "user" as const, content: "hello" }],
  };
  const sse = (frames: string[]) => {
    const encoder = new TextEncoder();
    return new ReadableStream<Uint8Array>({
      start(controller) {
        frames.forEach((frame) => controller.enqueue(encoder.encode(frame)));
        controller.close();
      },
    });
  };
  const hanging = () => vi.fn<typeof fetch>().mockImplementation((_input, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
  }));

  it("surfaces Core's rejection of a new message instead of an uncertain acceptance", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      detail: "conversation exceeds the model context window even after compaction",
      error_id: "err_ctx1",
    }), { status: 503, headers: { "content-type": "application/json" } }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    const events: string[] = [];

    const error = await client.streamChat(request, (event) => events.push(event.type)).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 503, errorId: "err_ctx1" });
    expect((error as Error).message).toBe("conversation exceeds the model context window even after compaction Reference: err_ctx1.");
    expect(events).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not record a cancelled request as a Core transport failure", async () => {
    diagnostics.logDiagnostic.mockClear();
    const fetchMock = hanging();
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    const controller = new AbortController();
    const pending = client.health(controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(diagnostics.logDiagnostic.mock.calls.filter(([record]) => record.level === "error")).toEqual([]);

    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(client.health()).rejects.toThrow("Failed to fetch");
    expect(diagnostics.logDiagnostic).toHaveBeenCalledWith(expect.objectContaining({
      level: "error",
      eventCode: "interface.api.transport_failed",
    }));
  });

  it("pages the Library through the shared list helper", async () => {
    const item = (id: string) => ({
      id, name: id, source_type: "document", status: "ready", document_count: 1,
      created_at: "2026-09-20T10:00:00Z", updated_at: "2026-09-20T10:00:00Z", revision: 1, metadata: {},
    });
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify(Array.from({ length: 1000 }, (_, index) => item(`library-${index}`))), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([item("library-1000")]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const result = await client.listLibraryItems();

    expect(result.total).toBe(1001);
    expect(result.items.at(-1)?.id).toBe("library-1000");
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:8765/api/v1/library/items?limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/library/items?limit=1000&offset=1000",
    ]);
  });

  it("waits for Core to prepare a new turn far longer than a reconnect header budget", async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = hanging();
      const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
      let settled: unknown;
      const attempt = client.streamChat(request, () => undefined)
        .then(() => { settled = "resolved"; }, (error: unknown) => { settled = error; });

      await vi.advanceTimersByTimeAsync(60_000);
      expect(settled).toBeUndefined();

      await vi.advanceTimersByTimeAsync(15 * 60_000);
      await attempt;
      expect(settled).toBeInstanceOf(Error);
      expect((settled as Error).message).toContain("lost before acceptance");
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("ends a followed turn cleanly when Core reports it was stopped", async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(sse([
        'event: started\ndata: {"type":"started","turn_id":"turn-1","provider_id":"provider-1","model":"model-1","session_id":"session-1","sequence":1}\n\n',
        'event: delta\ndata: {"type":"delta","turn_id":"turn-1","provider_id":"provider-1","model":"model-1","delta":"partial","sequence":2}\n\n',
        'event: cancelled\ndata: {"type":"cancelled","turn_id":"turn-1","detail":"response stopped"}\n\n',
      ]), { status: 200 }))
      .mockRejectedValue(new Error("no reconnect expected"));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    const events: ChatStreamEvent[] = [];

    const result = await client.followChatTurn("turn-1", request, (event) => events.push(event));

    expect(result).toBeUndefined();
    expect(events.map((event) => event.type)).toEqual(["connection", "started", "delta", "cancelled"]);
    expect(events.at(-1)).toEqual({ type: "cancelled", turnId: "turn-1", detail: "response stopped" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
