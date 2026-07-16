import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./client";

describe("ApiClient", () => {
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
    });
  });

  it("maps zero-setup readiness and refreshes runtime detection idempotently", async () => {
    const status = {
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
      detail: null,
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
      detail: undefined,
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
    expect(request.privacy).toEqual({ local_only: false, permits_sensitive_data: true });
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
      cve_ids: ["CVE-2026-1234"],
      cwe_ids: ["CWE-79"],
      metadata: { origin: "manual_operator_entry" },
    });
    expect(created).toMatchObject({ id: "finding-new", status: "candidate", verifierId: undefined, evidenceCount: 0 });
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
        privacy: { local_only: false, retention: "provider-policy", residency: ["us"], permits_sensitive_data: true },
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
      .mockResolvedValueOnce(new Response(JSON.stringify(source), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...source, revision: 2 }), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const listed = await client.listKnowledgeSources("engagement-1");
    const ingested = await client.ingestKnowledgeSource({
      engagementId: "engagement-1",
      filename: "scope.md",
      mediaType: "text/markdown",
      contentBase64: "IyBTY29wZQ==",
    });
    await client.reindexKnowledgeSource("knowledge-1");
    await client.deleteKnowledgeSource("knowledge-1");

    expect(listed.items[0]).toMatchObject({
      artifactId: "artifact-1",
      documentCount: 3,
      metadata: { filename: "scope.md", mediaType: "text/markdown", chunkCount: 3 },
    });
    expect(ingested.engagementId).toBe("engagement-1");
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:8765/api/v1/knowledge?engagement_id=engagement-1&limit=1000&offset=0",
      "http://127.0.0.1:8765/api/v1/knowledge/ingest",
      "http://127.0.0.1:8765/api/v1/knowledge/knowledge-1/reindex",
      "http://127.0.0.1:8765/api/v1/knowledge/knowledge-1",
    ]);
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      engagement_id: "engagement-1",
      filename: "scope.md",
      media_type: "text/markdown",
      content_base64: "IyBTY29wZQ==",
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
    const client = new ApiClient({
      baseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn<typeof fetch>().mockResolvedValue(new Response(stream, { status: 200 })),
    });
    const events: Array<{ type: string; phase?: string; harnessSessionId?: string; previousSessionId?: string }> = [];

    const result = await client.streamChat({
      harnessProfileId: "harness-1",
      harnessSessionId: "session-old",
      engagementId: "engagement-1",
      model: "model-1",
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
        metadata: {},
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

  it("maps provenance-backed chat and mission context status", async () => {
    const status = {
      owner_type: "chat_session",
      owner_id: "session-1",
      status: "ready",
      context_window: 8192,
      max_output_tokens: 2048,
      target_input_tokens: 4608,
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
    expect(mission.ownerType).toBe("agent_run");
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:8765/api/v1/chat/sessions/session-1/context",
      "http://127.0.0.1:8765/api/v1/runs/run-1/context",
    ]);
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
    const run = await client.createMission({ engagementId: engagement.id, objective: "Review scope", providerId: "provider-1", model: "model-1", maxDurationSeconds: 600, maxTokens: 2000, maxCostUsd: 2, maxRetries: 1 });
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

  it("sends analysis-only defaults and bounded command-runtime mission budgets", async () => {
    const response = { id: "run-1", engagement_id: "engagement-1", objective: "Review", status: "queued", created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T10:00:00Z", revision: 1, metadata: {} };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => new Response(JSON.stringify(response), { status: 202 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    await client.createMission({ engagementId: "engagement-1", objective: "Review", providerId: "provider-1", model: "model-1" });
    await client.createMission({ engagementId: "engagement-1", objective: "Scan", providerId: "provider-1", model: "model-1", maxToolCalls: 20, maxConcurrency: 2 });

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({ max_tool_calls: 0, max_concurrency: 1 });
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
        capabilities: { checked_at: entity.updated_at, harness_version: "0.144.0", models: ["gpt-test", "gpt-next"] },
      }]), { status: 200 });
      if (path.endsWith("/mcp-servers")) return new Response(JSON.stringify([{
        ...entity,
        id: "mcp-1",
        name: "workspace",
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
      localOnly: false,
      permitsSensitiveData: true,
      nativeCapabilities: {
        workspaceAccess: "read",
        shell: true,
        webSearch: true,
        subagents: true,
      },
    });
    expect(server).toMatchObject({ required: true, tools: [{ name: "read_file", readOnly: true }] });
    expect(session).toMatchObject({ harnessProfileId: "harness-1", mcpServerIds: ["mcp-1"] });
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
});
