import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "./client";
import { getButlerAuthority, getButlerOverview, updateButlerAuthority } from "./butlerOverview";

describe("getButlerOverview", () => {
  it("maps Core's project snapshot while preserving main and child identities", async () => {
    const request = vi.fn().mockResolvedValue({
      project_id: "a/b",
      authority: "watch",
      authority_revision: 4,
      delegate_actions: [], effective_actions: [],
      watched_goals: [{ goal_id: "child", session_id: "session one", parent_goal_id: "main",
        objective: "Check a boundary", status: "running", actor_role: "subagent",
        coverage: { report_capability: "unavailable", reason: "No adapter" } }],
      incidents: [{ id: "incident-1", incident_key: "machine:host-a", revision: 2, status: "open", summary: "Host unreachable",
        evidence_summary: ["Three connection attempts timed out"], evidence_refs: [], verification_state: "agent_reported" }],
      decisions: [{ id: "decision-1", summary: "Request parent status", choice: "request_parent_status", status: "queued",
        reason: "Checkpoint is overdue", impact: "Progress is uncertain", next_action: "Wait for the parent reply",
        source_verification_state: "core_observed", session_id: "session one", evidence_refs: [] }], last_event_sequence: 23,
      actions: [{ id: "action-1", action_type: "request_parent_status", status: "uncertain",
        summary: "Asked the parent for status", source_session_id: "session one", target_session_id: "session one",
        verification_state: "core_observed", started_at: "2026-10-15T16:00:00Z" }],
      next_action_offset: 50,
      resources: [{ kind: "openrouter_account", provider_id: "provider-1", provider_name: "OpenRouter",
        status: "unavailable", remaining_usd: null, observed_at: null, threshold_state: "unknown" },
      { kind: "harness_quota", harness_profile_id: "harness-1", harness_name: "Codex", status: "unavailable",
        remaining_percent: null, observed_at: null, threshold_state: "unknown", window_id: "weekly",
        window_label: "Weekly", resets_at: null, source: "codex_account" }],
      next_resource_offset: 50,
      coverage_gaps: ["No agent reports"],
      summary: { watched_goals: 1, open_incidents: 0, acknowledged_incidents: 0, resolved_incidents: 0, actions: 51, account_resources: 51, harness_quota_resources: 1 },
      capabilities: { authority_levels: ["watch"], incidents: false, decisions: false, actions: true, agent_reports: false, machine_alerts: true, resource_alerts: true, live_events: false },
    });
    const signal = new AbortController().signal;
    const result = await getButlerOverview({ request } as unknown as ApiClient, "a/b", signal);
    expect(request).toHaveBeenCalledWith("engagements/a%2Fb/butler/overview", { signal });
    expect(result.watchedGoals[0]).toMatchObject({
      goalId: "child", sessionId: "session one", parentGoalId: "main", actorRole: "subagent",
      coverage: { reportCapability: "unavailable", reason: "No adapter" },
    });
    expect(result.lastEventSequence).toBe(23);
    expect(result.authorityRevision).toBe(4);
    expect(result.incidents[0].evidenceSummary).toEqual(["Three connection attempts timed out"]);
    expect(result.decisions[0]).toMatchObject({ choice: "request_parent_status", reason: "Checkpoint is overdue",
      impact: "Progress is uncertain", nextAction: "Wait for the parent reply", sourceVerificationState: "core_observed" });
    expect(result.actions[0]).toMatchObject({ id: "action-1", status: "uncertain", actionType: "request_parent_status",
      sourceSessionId: "session one", targetSessionId: "session one", verificationState: "core_observed" });
    expect(result.summary.actions).toBe(51);
    expect(result.nextActionOffset).toBe(50);
    expect(result.capabilities.actions).toBe(true);
    expect(result.summary.watchedGoals).toBe(1);
    expect(result.capabilities.incidents).toBe(false);
    expect(result.capabilities.machineAlerts).toBe(true);
    expect(result.capabilities.resourceAlerts).toBe(true);
    expect(result.resources[0]).toEqual({ kind: "openrouter_account", providerId: "provider-1", providerName: "OpenRouter",
      status: "unavailable", remainingUsd: undefined, observedAt: undefined, thresholdState: "unknown" });
    expect(result.resources[1]).toEqual({ kind: "harness_quota", harnessProfileId: "harness-1", harnessName: "Codex",
      status: "unavailable", remainingPercent: undefined, observedAt: undefined, thresholdState: "unknown",
      windowId: "weekly", windowLabel: "Weekly", resetsAt: undefined, source: "codex_account" });
    expect(result.summary.accountResources).toBe(51);
    expect(result.summary.harnessQuotaResources).toBe(1);
    expect(result.nextResourceOffset).toBe(50);
  });

  it("rejects a response bound to another project", async () => {
    const request = vi.fn().mockResolvedValue({ project_id: "other", authority: "watch" });
    await expect(getButlerOverview({ request } as unknown as ApiClient, "selected"))
      .rejects.toThrow("different project");
  });

  it("reads and saves the project authority with a separate revision and explicit Delegate grant", async () => {
    const saved = { project_id: "a/b", authority: "watch", delegate_actions: [], effective_actions: [],
      supported_actions: ["request_parent_status", "pause_urgent_machine_blocker"], revision: 7 };
    const updated = { ...saved, authority: "delegate", delegate_actions: ["pause_urgent_machine_blocker"],
      effective_actions: ["request_parent_status", "pause_urgent_machine_blocker"], revision: 8 };
    const request = vi.fn().mockResolvedValueOnce(saved).mockResolvedValueOnce(updated);
    const api = { request } as unknown as ApiClient;
    const policy = await getButlerAuthority(api, "a/b");
    expect(policy).toMatchObject({ projectId: "a/b", revision: 7, authority: "watch" });
    expect(request).toHaveBeenNthCalledWith(1, "engagements/a%2Fb/butler/authority", { signal: undefined });

    const result = await updateButlerAuthority(api, policy, "delegate", ["pause_urgent_machine_blocker"]);
    expect(result).toMatchObject({ authority: "delegate", revision: 8, delegateActions: ["pause_urgent_machine_blocker"] });
    expect(request).toHaveBeenNthCalledWith(2, "engagements/a%2Fb/butler/authority", {
      method: "PUT", body: JSON.stringify({ authority: "delegate", delegate_actions: ["pause_urgent_machine_blocker"], expected_revision: 7 }),
    });
  });

  it("clears Delegate grants when authority is lowered and rejects another project's policy", async () => {
    const policy = { projectId: "selected", authority: "delegate" as const, delegateActions: ["pause_urgent_machine_blocker" as const],
      effectiveActions: ["request_parent_status", "pause_urgent_machine_blocker"], supportedActions: ["request_parent_status", "pause_urgent_machine_blocker"], revision: 3 };
    const request = vi.fn().mockResolvedValueOnce({ project_id: "selected", authority: "watch", delegate_actions: [],
      effective_actions: [], supported_actions: policy.supportedActions, revision: 4 })
      .mockResolvedValueOnce({ project_id: "different", authority: "watch", delegate_actions: [],
        effective_actions: [], supported_actions: policy.supportedActions, revision: 4 });
    const api = { request } as unknown as ApiClient;
    await updateButlerAuthority(api, policy, "watch", ["pause_urgent_machine_blocker"]);
    expect(request).toHaveBeenNthCalledWith(1, "engagements/selected/butler/authority", {
      method: "PUT", body: JSON.stringify({ authority: "watch", delegate_actions: [], expected_revision: 3 }),
    });
    await expect(getButlerAuthority(api, "selected")).rejects.toThrow("different project");
  });
});
