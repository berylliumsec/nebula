import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { ButlerOversight } from "./ButlerOversight";

const overview = {
  project_id: "project-a",
  authority: "watch",
  watched_goals: [{
    goal_id: "goal-main", session_id: "chat-main", parent_goal_id: null,
    objective: "Investigate the parser", status: "running", actor_role: "main_agent",
    progress_due_at: "2026-10-15T15:00:00Z",
    coverage: { report_capability: "turn_output_only", reason: "Completed goal turns receive bounded semantic review. Structured harness progress reports are not connected yet." },
  }, {
    goal_id: "goal-child", session_id: "chat-child", parent_goal_id: "goal-main",
    objective: "Check the decoder", status: "running", actor_role: "subagent",
    coverage: { report_capability: "unavailable", reason: "Harness has no reporting adapter" },
  }],
  incidents: [], decisions: [], last_event_sequence: 8,
  coverage_gaps: ["Harness progress reports are not validated yet"],
  summary: { watched_goals: 2, open_incidents: 0, acknowledged_incidents: 0, resolved_incidents: 0 },
  capabilities: { authority_levels: ["watch"], incidents: false, decisions: false, agent_reports: false, machine_alerts: false, resource_alerts: false, live_events: false },
};

function client(request: ReturnType<typeof vi.fn>): ApiClient {
  const readOverview = request as unknown as (path: string, options?: unknown) => Promise<unknown>;
  return { request: (path: string, options?: unknown) => path.endsWith("/butler/issues")
    ? Promise.resolve({ destinations: [], candidates: [], held_scans: [] })
    : readOverview(path, options) } as unknown as ApiClient;
}

describe("ButlerOversight", () => {
  it("shows the Nebula-wide user file path and refreshes its scheduled state", async () => {
    const user = userEvent.setup();
    const path = "/tmp/nebula-data/.butler/BUTLER.md";
    const request = vi.fn()
      .mockResolvedValueOnce({ ...overview, global_instructions: { status: "ready", path, last_check_at: "2026-10-18T12:00:00Z" } })
      .mockResolvedValue({ ...overview, global_instructions: { status: "missing", path } });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);
    await user.click(await screen.findByText("Nebula-wide Butler"));
    expect(screen.getByText(path)).toBeVisible();
    expect(screen.getByText(/Butler checks once every 15 minutes/)).toBeVisible();
    expect(screen.getByText("Last check:")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Refresh Butler oversight" }));
    expect(await screen.findByText("No file")).toBeVisible();
    expect(screen.getByText(path)).toBeVisible();
  });

  it("opens the affected mission from a pending review incident", async () => {
    const request = vi.fn().mockResolvedValue({
      ...overview,
      capabilities: { ...overview.capabilities, incidents: true },
      summary: { ...overview.summary, open_incidents: 1 },
      incidents: [{ id: "review-1", incident_key: "review:approval:approval-1:approval-1", revision: 1,
        status: "open", summary: "An agent request needs approval.", source: "pending_approval",
        verification_state: "core_observed", evidence_refs: [
          { kind: "approval", id: "approval-1", project_id: "project-a" },
          { kind: "mission", id: "mission-1", project_id: "project-a" },
        ], session_ids: [], goal_ids: [] }],
    });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" compact />);
    const incident = (await screen.findByText("An agent request needs approval.")).closest("li")!;
    expect(within(incident).getByRole("link", { name: "Open mission" })).toHaveAttribute(
      "href", "/projects/project-a/workbench?view=missions&mission=mission-1",
    );
  });

  it("shows Act recovery status and a separate scheduled activity section", async () => {
    const request = vi.fn().mockResolvedValue({
      ...overview,
      authority: "act",
      capabilities: { ...overview.capabilities, recovery: true, authority_levels: ["watch", "act"] },
      summary: { ...overview.summary, recovery_cases: 2, recovery_active: 1, recovery_attention: 0 },
      recovery_cases: [
        { id: "case-active", trigger: "immediate", signal: "agent.subagent_start_failed",
          status: "verifying", summary: "Subagent start failed", outcome: "Parent retry queued",
          target_session_id: "chat-main", steps: [{ id: "step-1", operation: "nebula.work.message_chat", status: "applied" }] },
        { id: "case-done", trigger: "periodic", signal: "butler.periodic_check",
          status: "resolved", summary: "Scheduled work check", outcome: "Work is progressing", steps: [] },
      ],
    });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);
    const recovery = await screen.findByRole("region", { name: "Butler recovery" });
    expect(within(recovery).getByText("Subagent start failed")).toBeInTheDocument();
    expect(within(recovery).getByText("Parent retry queued")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Butler scheduled activity" })).toHaveTextContent("Scheduled work check");
    expect(within(recovery).queryByText("Scheduled work check")).not.toBeInTheDocument();
    expect(within(recovery).getByRole("link", { name: /Open parent chat/ })).toHaveAttribute("href", "/projects/project-a/workbench?view=chat&session=chat-main");
  });

  it("reopens saved scheduled transcript and recovery report after a retry and remount", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValue({ ...overview, authority: "act",
      capabilities: { ...overview.capabilities, recovery: true },
      recovery_cases: [{ id: "scheduled", trigger: "periodic", signal: "butler.global_periodic_check",
        status: "resolved", summary: "Scheduled work check", outcome: "Created a recovery goal",
        model_backend: "harness", model_turn_id: "turn-saved", steps: [],
        recovery_goals: [{ id: "goal-recovery", project_id: "project-a", session_id: "chat-recovery",
          source_goal_id: "goal-main", objective: "Unblock parser goal", status: "running",
          current_step: 2, updated_at: "2026-10-19T12:00:00Z", report: "Inspecting saved state." }] }],
    });
    const events = vi.fn().mockRejectedValueOnce(new Error("connection lost")).mockResolvedValue({ events: [{
      schemaVersion: "nebula.harness-activity/v2", sequence: 1, type: "tool_started", vendor: "codex_app_server",
      harnessTurnId: "turn-saved", itemId: "tool-1", itemKind: "tool", title: "Read Core goals",
      artifactIds: [], payload: {},
    }], nextSequence: 1 });
    const turn = vi.fn().mockResolvedValue({ id: "turn-saved", status: "complete", origin: "analysis",
      harnessSessionId: "analysis-session", response: "I inspected the paused goal and started recovery." });
    const api = { ...client(request), getHarnessTurnEvents: events, getHarnessTurn: turn } as unknown as ApiClient;
    const first = render(<ButlerOversight api={api} projectId="project-a" coreState="online" />);
    await user.click(await screen.findByText("Check transcript"));
    expect(await screen.findByText(/connection lost/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("I inspected the paused goal and started recovery.")).toBeVisible();
    expect(screen.getByRole("region", { name: "Butler check activity" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open recovery goal" })).toHaveAttribute("href", "/projects/project-a/workbench?view=chat&session=chat-recovery");
    first.unmount();
    render(<ButlerOversight api={api} projectId="project-a" coreState="online" />);
    await user.click(await screen.findByText("Check transcript"));
    expect(await screen.findByText("I inspected the paused goal and started recovery.")).toBeVisible();
    expect(events).toHaveBeenCalledTimes(3);
  });

  it("keeps Watch compact beside the chat while surfacing open incidents", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValue({
      ...overview,
      capabilities: { ...overview.capabilities, incidents: true },
      summary: { ...overview.summary, open_incidents: 1 },
      incidents: [{ id: "incident-1", incident_key: "machine:host-a", revision: 1, status: "open",
        summary: "Host unreachable", source: "agent_report", verification_state: "agent_reported",
        evidence_refs: [], impact: "Research is blocked", next_action: "Check host power",
        session_ids: ["chat-main"], goal_ids: ["goal-main"] }],
    });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" compact />);

    const toggle = screen.getByRole("button", { name: /Oversight/ });
    expect(await screen.findByText("2 watched · 1 need attention")).toBeVisible();
    const incident = screen.getByText("Host unreachable").closest("li")!;
    expect(incident).toHaveTextContent("Impact: Research is blocked");
    expect(incident).toHaveTextContent("Next: Check host power");
    expect(within(incident).getByRole("link", { name: "Open chat" })).toBeVisible();
    expect(within(incident).getByRole("button", { name: "Acknowledge" })).toBeEnabled();
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/2 watched goals · Watch/)).not.toBeInTheDocument();

    await user.click(toggle);
    expect(screen.getByText("2 watched goals · Watch")).toBeVisible();
    expect(screen.getByRole("button", { name: "Collapse Butler oversight" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Collapse Butler oversight" }));
    expect(screen.getByText("2 watched · 1 need attention")).toBeVisible();
  });

  it("shows automatic Watch and reporting gaps without claiming unavailable incident tracking is healthy", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValue(overview);
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);

    expect(await screen.findByText("2 watched goals · Watch")).toBeVisible();
    expect(screen.getByText("Incident alerts are not active yet. Watch still tracks goal state.")).toBeVisible();
    expect(screen.queryByText("No incidents recorded.")).not.toBeInTheDocument();
    await user.click(screen.getByText(/^Watched goals/));
    const watched = screen.getByText("Investigate the parser").closest("li")!;
    expect(within(watched).getByText("Main agent · running")).toBeVisible();
    expect(within(watched).getByText(/Turn output only.*Completed goal turns receive bounded semantic review/)).toBeVisible();
    const child = screen.getByText("Check the decoder").closest("li")!;
    expect(within(child).getByText("Subagent · running")).toBeVisible();
    expect(within(child).getByText("Reports through main agent · Investigate the parser")).toBeVisible();
    expect(within(child).getByRole("link", { name: "Open chat" })).toHaveAttribute("href", "/projects/project-a/workbench?view=chat&session=chat-child");
    await user.click(screen.getByText("Oversight settings"));
    expect(screen.getByText("Authority:").closest("p")).toHaveTextContent("Watch · Butler watches running goals automatically.");
    expect(screen.queryByRole("combobox", { name: "Butler authority" })).not.toBeInTheDocument();
    expect(screen.getByText("Harness progress reports are not validated yet")).toBeVisible();
    expect(request).toHaveBeenCalledWith("engagements/project-a/butler/overview", { signal: expect.any(AbortSignal) });
  });

  it("shows actionable incidents and decisions with source, evidence, and affected work", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValue({
      ...overview,
      capabilities: { ...overview.capabilities, incidents: true, decisions: true, machine_alerts: true },
      watched_goals: [{ ...overview.watched_goals[0], coverage: { report_capability: "validated", reason: "Report claims remain agent-reported" } }, overview.watched_goals[1]],
      summary: { ...overview.summary, open_incidents: 1 },
      incidents: [{ id: "incident-1", incident_key: "machine:host-a", revision: 1, status: "open", summary: "Host unreachable", source: "agent_report",
        verification_state: "agent_reported", evidence_refs: [{ kind: "agent_report", id: "report-1", project_id: "project-a" }],
        evidence_summary: ["Three connection attempts timed out"], impact: "Research is blocked",
        next_action: "Check host power", session_ids: ["chat-child"], goal_ids: ["goal-child"] }],
      decisions: [{ id: "decision-1", summary: "Asked agent for a new hypothesis", choice: "request_parent_status", status: "queued",
        reason: "Checkpoint is overdue", impact: "Progress is uncertain", next_action: "Wait for a reply",
        source_verification_state: "core_observed", evidence_refs: [], session_id: "chat-main" }],
    });
    const onNavigate = vi.fn();
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" onNavigate={onNavigate} />);

    const incident = (await screen.findByText("Host unreachable")).closest("li")!;
    expect(within(incident).getByText("agent_report · agent reported")).toBeVisible();
    expect(within(incident).getByText("Impact:")).toBeVisible();
    expect(within(incident).getByText("Next:")).toBeVisible();
    await user.click(within(incident).getByText("Agent-reported evidence · 1"));
    expect(within(incident).getByText("Three connection attempts timed out")).toBeVisible();
    await user.click(within(incident).getByText("Evidence references"));
    expect(within(incident).getByText("report-1")).toBeVisible();
    expect(within(incident).getByText(/Affected goals:.*Check the decoder/)).toBeVisible();
    await user.click(within(incident).getByRole("link", { name: "Open chat" }));
    expect(onNavigate).toHaveBeenCalledWith("/projects/project-a/workbench?view=chat&session=chat-child");
    await user.click(screen.getByText(/^Recent decisions/));
    expect(screen.getByText("Asked agent for a new hypothesis")).toBeVisible();
    expect(screen.getByText(/request parent status.*queued/)).toBeVisible();
    expect(screen.getByText("Checkpoint is overdue")).toBeVisible();
    expect(screen.getByText("Wait for a reply")).toBeVisible();
    await user.click(screen.getByText(/^Watched goals/));
    expect(screen.getByText(/Structured agent reports.*Report claims remain agent-reported/)).toBeVisible();
    await user.click(screen.getByText("Oversight settings"));
    expect(screen.getByText(/Claims remain agent-reported until verified/)).toBeVisible();
  });

  it("clears the previous project's snapshot while the new project loads", async () => {
    const request = vi.fn().mockImplementation((path: string) => path.includes("project-a")
      ? Promise.resolve(overview)
      : new Promise(() => {}));
    const api = client(request);
    const view = render(<ButlerOversight api={api} projectId="project-a" coreState="online" />);
    expect(await screen.findByText("2 watched goals · Watch")).toBeVisible();
    view.rerender(<ButlerOversight api={api} projectId="project-b" coreState="online" />);
    expect(screen.queryByText("2 watched goals · Watch")).not.toBeInTheDocument();
    expect(screen.getByText("Loading oversight…")).toBeVisible();
    expect(request).toHaveBeenCalledWith("engagements/project-b/butler/overview", { signal: expect.any(AbortSignal) });
  });

  it("keeps saved project state through an offline interval and reloads after reconnect", async () => {
    const request = vi.fn().mockResolvedValueOnce(overview).mockResolvedValueOnce({
      ...overview, watched_goals: [], summary: { ...overview.summary, watched_goals: 0 },
    });
    const api = client(request);
    const view = render(<ButlerOversight api={api} projectId="project-a" coreState="online" />);
    expect(await screen.findByText("2 watched goals · Watch")).toBeVisible();
    view.rerender(<ButlerOversight api={api} projectId="project-a" coreState="offline" />);
    expect(screen.getByText(/Core is offline.*Showing the last overview/)).toBeVisible();
    expect(screen.getByText("2 watched goals · Watch")).toBeVisible();
    view.rerender(<ButlerOversight api={api} projectId="project-a" coreState="online" />);
    await waitFor(() => expect(screen.getByText("0 watched goals · Watch")).toBeVisible());
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("offers Retry after a failed read and recovers from Core", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockRejectedValueOnce(new Error("Connection failed")).mockResolvedValueOnce(overview);
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection failed");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("2 watched goals · Watch")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("acknowledges an incident with its saved revision and reloads Core's status", async () => {
    const user = userEvent.setup();
    const incident = { id: "incident-1", incident_key: "machine:host-a", revision: 3, status: "open",
      summary: "Host unreachable", source: "agent_report", verification_state: "agent_reported",
      evidence_refs: [], impact: "Research is blocked", next_action: "Check host power",
      session_ids: ["chat-main"], goal_ids: ["goal-main"] };
    const request = vi.fn()
      .mockResolvedValueOnce({ ...overview, capabilities: { ...overview.capabilities, incidents: true },
        summary: { ...overview.summary, open_incidents: 1 }, incidents: [incident] })
      .mockResolvedValueOnce({ ...incident, status: "acknowledged", revision: 4 })
      .mockResolvedValueOnce({ ...overview, capabilities: { ...overview.capabilities, incidents: true },
        summary: { ...overview.summary, acknowledged_incidents: 1 }, incidents: [{ ...incident, status: "acknowledged", revision: 4 }] });
    const onIncidentChanged = vi.fn();
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" compact onIncidentChanged={onIncidentChanged} />);

    await user.click(await screen.findByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(screen.getByText("2 watched · 0 need attention")).toBeVisible());
    expect(request).toHaveBeenCalledWith("engagements/project-a/butler/incidents/incident-1/acknowledge", {
      method: "POST", body: JSON.stringify({ expected_revision: 3 }),
    });
    expect(screen.queryByRole("button", { name: "Acknowledge" })).not.toBeInTheDocument();
    expect(onIncidentChanged).toHaveBeenCalledOnce();
  });

  it("refreshes a changed incident before allowing a second acknowledgment", async () => {
    const user = userEvent.setup();
    const incident = { id: "incident-1", incident_key: "machine:host-a", revision: 3, status: "open",
      summary: "Host unreachable", source: "agent_report", verification_state: "agent_reported",
      evidence_refs: [], evidence_summary: ["First attempt timed out"], impact: "Research is blocked",
      next_action: "Check host power", session_ids: ["chat-main"], goal_ids: ["goal-main"] };
    const request = vi.fn()
      .mockResolvedValueOnce({ ...overview, capabilities: { ...overview.capabilities, incidents: true },
        summary: { ...overview.summary, open_incidents: 1 }, incidents: [incident] })
      .mockRejectedValueOnce(new ApiError("incident changed", 409))
      .mockResolvedValueOnce({ ...overview, capabilities: { ...overview.capabilities, incidents: true },
        summary: { ...overview.summary, open_incidents: 1 }, incidents: [{ ...incident, revision: 4,
          evidence_summary: ["First attempt timed out", "Second attempt timed out"] }] })
      .mockResolvedValueOnce({ ...incident, revision: 5, status: "acknowledged" })
      .mockResolvedValueOnce({ ...overview, capabilities: { ...overview.capabilities, incidents: true },
        summary: { ...overview.summary, open_incidents: 0, acknowledged_incidents: 1 },
        incidents: [{ ...incident, revision: 5, status: "acknowledged" }] });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);

    await user.click(await screen.findByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByText(/This incident changed\. Review its latest details, then acknowledge it\./)).toBeVisible();
    expect(await screen.findByText("Agent-reported evidence · 2")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: /Needs attention/ })).toHaveTextContent("1 acknowledged"));
    const posts = request.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts.map(([, init]) => JSON.parse(init.body).expected_revision)).toEqual([3, 4]);
  });

  it("keeps an incident visible while offline and explains how to acknowledge it", async () => {
    const incident = { id: "incident-1", incident_key: "machine:host-a", revision: 1, status: "open",
      summary: "Host unreachable", source: "agent_report", verification_state: "agent_reported", evidence_refs: [],
      impact: "Research is blocked", next_action: "Check host power", session_ids: [], goal_ids: [] };
    const request = vi.fn().mockResolvedValue({ ...overview,
      capabilities: { ...overview.capabilities, incidents: true },
      summary: { ...overview.summary, open_incidents: 1 }, incidents: [incident],
    });
    const api = client(request);
    const view = render(<ButlerOversight api={api} projectId="project-a" coreState="online" compact />);
    expect(await screen.findByText("Host unreachable")).toBeVisible();
    view.rerender(<ButlerOversight api={api} projectId="project-a" coreState="offline" compact />);
    expect(screen.getByRole("button", { name: "Acknowledge" })).toBeDisabled();
    expect(screen.getByText("Reconnect to Core to acknowledge this incident.")).toBeVisible();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("keeps expanded empty oversight concise while preserving the Watch entry path", async () => {
    const request = vi.fn().mockResolvedValue({ ...overview, watched_goals: [],
      capabilities: { ...overview.capabilities, incidents: true, resource_alerts: true },
      summary: { ...overview.summary, watched_goals: 0, account_resources: 0, harness_quota_resources: 0 },
    });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);
    expect(await screen.findByText("0 watched goals · Watch")).toBeVisible();
    expect(screen.getByText("Start or resume a goal to begin Watch.")).toBeVisible();
    expect(screen.queryByText("Resources")).not.toBeInTheDocument();
    expect(screen.queryByText("Recent decisions")).not.toBeInTheDocument();
    expect(screen.queryByText("Watched goals")).not.toBeInTheDocument();
  });

  it("loads the next watched-goal page without dropping the first page", async () => {
    const user = userEvent.setup();
    const first = { ...overview, watched_goals: [overview.watched_goals[0]], next_watch_offset: 1 };
    const second = { ...overview, watched_goals: [overview.watched_goals[1]], next_watch_offset: null };
    const request = vi.fn().mockResolvedValueOnce(first).mockResolvedValueOnce(second);
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);

    await user.click(await screen.findByText(/^Watched goals/));
    expect(screen.getByText("Investigate the parser")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Show more goals" }));
    expect(await screen.findByText("Check the decoder")).toBeVisible();
    expect(screen.getByText("Investigate the parser")).toBeVisible();
    expect(request).toHaveBeenCalledWith("engagements/project-a/butler/overview?watch_offset=1", { signal: expect.any(AbortSignal) });
  });

  it("shows observed and unavailable account balances without inventing zero, and pages project observations", async () => {
    const user = userEvent.setup();
    const first = { ...overview, capabilities: { ...overview.capabilities, resource_alerts: true },
      summary: { ...overview.summary, account_resources: 3 }, next_resource_offset: 2,
      resources: [{ kind: "openrouter_account", provider_id: "provider-low", provider_name: "Primary OpenRouter",
        status: "observed", remaining_usd: 18.5, observed_at: "2026-10-15T16:00:00Z", threshold_state: "at_or_below" },
      { kind: "openrouter_account", provider_id: "provider-missing", provider_name: "Backup OpenRouter",
        status: "unavailable", remaining_usd: null, observed_at: null, threshold_state: "unknown" }] };
    const second = { ...first, next_resource_offset: null,
      resources: [{ kind: "openrouter_account", provider_id: "provider-third", provider_name: "Third OpenRouter",
        status: "observed", remaining_usd: 39, observed_at: "2026-10-15T17:00:00Z", threshold_state: "above" }] };
    const request = vi.fn().mockResolvedValueOnce(first).mockResolvedValueOnce(second);
    const onNavigate = vi.fn();
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" onNavigate={onNavigate} />);

    const low = (await screen.findByText("Primary OpenRouter")).closest("li")!;
    expect(within(low).getByText(/\$18\.50 remaining/)).toBeVisible();
    expect(within(low).getByText(/At or below account threshold/)).toBeVisible();
    expect(within(low).getByText(/Balance may change between checks/)).toBeVisible();
    const unavailable = screen.getByText("Backup OpenRouter").closest("li")!;
    expect(within(unavailable).getByText("Account balance unavailable.")).toBeVisible();
    expect(within(unavailable).queryByText(/\$0/)).not.toBeInTheDocument();
    await user.click(within(unavailable).getByRole("link", { name: "Model providers" }));
    expect(onNavigate).toHaveBeenCalledWith("/settings#provider-settings");
    await user.click(screen.getByRole("button", { name: "Show more resources" }));
    expect(await screen.findByText("Third OpenRouter")).toBeVisible();
    expect(screen.getByText("Primary OpenRouter")).toBeVisible();
    expect(request).toHaveBeenCalledWith("engagements/project-a/butler/overview?resource_offset=2", { signal: expect.any(AbortSignal) });
  });

  it("shows measured harness quota and keeps missing numeric quota unknown", async () => {
    const request = vi.fn().mockResolvedValue({ ...overview,
      capabilities: { ...overview.capabilities, resource_alerts: true },
      summary: { ...overview.summary, account_resources: 0, harness_quota_resources: 2 },
      resources: [{ kind: "harness_quota", harness_profile_id: "codex", harness_name: "Codex harness",
        status: "observed", remaining_percent: 19.5, observed_at: "2026-10-15T16:00:00Z",
        threshold_state: "at_or_below", window_id: "weekly", window_label: "Weekly",
        resets_at: "2026-10-22T16:00:00Z", source: "codex_account" },
      { kind: "harness_quota", harness_profile_id: "grok", harness_name: "Grok harness",
        status: "unavailable", remaining_percent: null, observed_at: null,
        threshold_state: "unknown", window_id: "current", window_label: "Current window",
        resets_at: null, source: "grok_harness" }],
    });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);
    const measured = (await screen.findByText("Codex harness")).closest("li")!;
    expect(within(measured).getByText(/19\.5% remaining/)).toBeVisible();
    expect(within(measured).getByText(/At or below 20% quota threshold/)).toBeVisible();
    expect(within(measured).getByText(/Resets/)).toBeVisible();
    const unknown = screen.getByText("Grok harness").closest("li")!;
    expect(within(unknown).getByText("Quota remaining unknown. No numeric balance was reported.")).toBeVisible();
    expect(within(unknown).queryByText(/0% remaining/)).not.toBeInTheDocument();
  });

  it("shows agent-reported evidence behind a compact disclosure", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValue({ ...overview,
      capabilities: { ...overview.capabilities, incidents: true },
      summary: { ...overview.summary, open_incidents: 1 },
      incidents: [{ id: "incident-1", incident_key: "machine:host-a", revision: 1, status: "open",
        summary: "Host unreachable", source: "agent_report", verification_state: "agent_reported",
        evidence_summary: ["Three connection attempts timed out", "Host did not answer a status probe"],
        evidence_refs: [], impact: "Research is blocked", next_action: "Check host power",
        session_ids: [], goal_ids: [] }],
    });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" compact />);
    const row = (await screen.findByText("Host unreachable")).closest("li")!;
    const evidence = within(row).getByText("Agent-reported evidence · 2").closest("details")!;
    expect(evidence).not.toHaveAttribute("open");
    await user.click(within(row).getByText("Agent-reported evidence · 2"));
    expect(evidence).toHaveAttribute("open");
    expect(within(row).getByText("Three connection attempts timed out")).toBeVisible();
    expect(within(row).getByText("Host did not answer a status probe")).toBeVisible();
  });

  it("saves Steer from Core's project authority revision and refreshes the visible overview", async () => {
    const user = userEvent.setup();
    let authority = "watch";
    let revision = 4;
    const request = vi.fn().mockImplementation((path: string, init?: RequestInit) => {
      if (path.endsWith("/overview")) return Promise.resolve({ ...overview, authority,
        authority_revision: revision, capabilities: { ...overview.capabilities, authority_levels: ["watch", "steer", "delegate"] } });
      if (path.endsWith("/authority") && init?.method === "PUT") {
        authority = JSON.parse(String(init.body)).authority;
        revision += 1;
      }
      return Promise.resolve({ project_id: "project-a", authority, revision, delegate_actions: [],
        effective_actions: authority === "watch" ? [] : ["request_parent_status"],
        supported_actions: ["request_parent_status", "pause_urgent_machine_blocker"] });
    });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);
    await user.click(await screen.findByText("Oversight settings"));
    const select = await screen.findByRole("combobox", { name: "Butler authority" });
    expect(within(select).getAllByRole("option")).toHaveLength(3);
    await user.selectOptions(select, "steer");
    expect(screen.getByText(/request a status or clarification from the parent main agent/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save authority" }));
    await waitFor(() => expect(screen.getByText("2 watched goals · Steer")).toBeVisible());
    expect(request).toHaveBeenCalledWith("engagements/project-a/butler/authority", {
      method: "PUT", body: JSON.stringify({ authority: "steer", delegate_actions: [], expected_revision: 4 }),
    });
    expect(screen.getByText("Authority saved for this project.")).toBeVisible();
  });

  it("requires an explicit Delegate grant for urgent pause and disables changes offline", async () => {
    const user = userEvent.setup();
    const policy = { project_id: "project-a", authority: "watch", revision: 2, delegate_actions: [], effective_actions: [],
      supported_actions: ["request_parent_status", "pause_urgent_machine_blocker"] };
    const request = vi.fn().mockImplementation((path: string, init?: RequestInit) => path.endsWith("/overview")
      ? Promise.resolve({ ...overview, capabilities: { ...overview.capabilities, authority_levels: ["watch", "steer", "delegate"] } })
      : Promise.resolve(init?.method === "PUT" ? { ...policy, authority: "delegate", revision: 3,
        delegate_actions: ["pause_urgent_machine_blocker"], effective_actions: policy.supported_actions } : policy));
    const api = client(request);
    const view = render(<ButlerOversight api={api} projectId="project-a" coreState="online" />);
    await user.click(await screen.findByText("Oversight settings"));
    const select = await screen.findByRole("combobox", { name: "Butler authority" });
    await user.selectOptions(select, "delegate");
    expect(screen.getByText(/urgent pause requires the separate grant/)).toBeVisible();
    const grant = screen.getByRole("checkbox", { name: /Allow urgent pause of a running main goal/ });
    expect(grant).not.toBeChecked();
    await user.click(grant);
    expect(screen.getByText(/pause is reversible and audited/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save authority" }));
    expect(request).toHaveBeenCalledWith("engagements/project-a/butler/authority", {
      method: "PUT", body: JSON.stringify({ authority: "delegate", delegate_actions: ["pause_urgent_machine_blocker"], expected_revision: 2 }),
    });
    view.rerender(<ButlerOversight api={api} projectId="project-a" coreState="offline" />);
    expect(screen.getByRole("combobox", { name: "Butler authority" })).toBeDisabled();
    expect(screen.getByText("Reconnect to Core to change authority.")).toBeVisible();
  });

  it("saves Butler's one-task planning grant separately from urgent pause", async () => {
    const user = userEvent.setup();
    const policy = { project_id: "project-a", authority: "watch", revision: 2, delegate_actions: [], effective_actions: [],
      supported_actions: ["request_parent_status", "pause_urgent_machine_blocker", "force_one_task_plan"] };
    const request = vi.fn().mockImplementation((path: string, init?: RequestInit) => path.endsWith("/overview")
      ? Promise.resolve({...overview, capabilities: {...overview.capabilities, authority_levels: ["watch", "steer", "delegate"]}})
      : Promise.resolve(init?.method === "PUT" ? {...policy, authority: "delegate", revision: 3,
        delegate_actions: ["force_one_task_plan"], effective_actions: policy.supported_actions} : policy));
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);
    await user.click(await screen.findByText("Oversight settings"));
    await user.selectOptions(await screen.findByRole("combobox", {name: "Butler authority"}), "delegate");
    const force = screen.getByRole("checkbox", {name: /Allow Butler to require one Core plan/});
    expect(force).not.toBeChecked();
    await user.click(force);
    await user.click(screen.getByRole("button", {name: "Save authority"}));
    expect(request).toHaveBeenCalledWith("engagements/project-a/butler/authority", {
      method: "PUT", body: JSON.stringify({authority: "delegate", delegate_actions: ["force_one_task_plan"], expected_revision: 2}),
    });
    expect(force).toBeChecked();
  });

  it("offers a saved-policy reload after a stale authority revision", async () => {
    const user = userEvent.setup();
    let revision = 1;
    let writes = 0;
    const request = vi.fn().mockImplementation((path: string, init?: RequestInit) => {
      if (path.endsWith("/overview")) return Promise.resolve({ ...overview,
        capabilities: { ...overview.capabilities, authority_levels: ["watch", "steer"] } });
      if (init?.method === "PUT") {
        writes += 1;
        if (writes === 1) { revision = 2; return Promise.reject(new ApiError("changed", 409)); }
        return Promise.resolve({ project_id: "project-a", authority: "steer", revision: 3, delegate_actions: [],
          effective_actions: ["request_parent_status"], supported_actions: ["request_parent_status"] });
      }
      return Promise.resolve({ project_id: "project-a", authority: "watch", revision, delegate_actions: [],
        effective_actions: [], supported_actions: ["request_parent_status"] });
    });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);
    await user.click(await screen.findByText("Oversight settings"));
    await user.selectOptions(await screen.findByRole("combobox", { name: "Butler authority" }), "steer");
    await user.click(screen.getByRole("button", { name: "Save authority" }));
    expect(await screen.findByText(/Authority changed elsewhere/)).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Butler authority" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Reload saved authority" }));
    await user.selectOptions(await screen.findByRole("combobox", { name: "Butler authority" }), "steer");
    await user.click(screen.getByRole("button", { name: "Save authority" }));
    expect(request).toHaveBeenCalledWith("engagements/project-a/butler/authority", {
      method: "PUT", body: JSON.stringify({ authority: "steer", delegate_actions: [], expected_revision: 2 }),
    });
  });

  it("keeps an uncertain Butler action and its recovery visible in the compact view", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValue({ ...overview,
      capabilities: { ...overview.capabilities, actions: true },
      summary: { ...overview.summary, actions: 1 },
      actions: [{ id: "action-1", action_type: "request_parent_status", status: "uncertain",
        summary: "Requested an update from the parent", verification_state: "core_observed",
        source_session_id: "chat-main", target_session_id: "chat-main", started_at: "2026-10-15T16:00:00Z" }],
    });
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" compact />);
    expect(await screen.findByText("2 watched · 1 need attention")).toBeVisible();
    const action = screen.getByText("Requested an update from the parent").closest("li")!;
    expect(within(action).getByText(/Core cannot confirm whether this action was delivered/)).toBeVisible();
    expect(within(action).getByRole("link", { name: "Open parent chat" })).toHaveAttribute("href", "/projects/project-a/workbench?view=chat&session=chat-main");
    await user.click(screen.getByRole("button", { name: /Oversight/ }));
    expect(screen.getByText(/^Recent actions/)).toBeVisible();
  });

  it("pages recent actions while keeping uncertain work visible above older history", async () => {
    const user = userEvent.setup();
    const makeAction = (id: string, status = "applied") => ({ id, action_type: "request_parent_status", status,
      summary: `Action ${id}`, source_session_id: "chat-main", target_session_id: "chat-main" });
    const first = { ...overview, capabilities: { ...overview.capabilities, actions: true },
      summary: { ...overview.summary, actions: 6 }, next_action_offset: 5,
      actions: [makeAction("1"), makeAction("2"), makeAction("3"), makeAction("4"), makeAction("5", "uncertain")] };
    const second = { ...first, next_action_offset: null, actions: [makeAction("6")] };
    const request = vi.fn().mockResolvedValueOnce(first).mockResolvedValueOnce(second);
    render(<ButlerOversight api={client(request)} projectId="project-a" coreState="online" />);
    expect(await screen.findByText("Action 5")).toBeVisible();
    expect(screen.getByText("Action 1")).toBeVisible();
    await user.click(screen.getByText("More actions"));
    expect(screen.getByText("Action 4")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Show more actions" }));
    expect(await screen.findByText("Action 6")).toBeVisible();
    expect(request).toHaveBeenCalledWith("engagements/project-a/butler/overview?action_offset=5", { signal: expect.any(AbortSignal) });
  });
});
