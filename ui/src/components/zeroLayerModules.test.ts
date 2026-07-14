import { describe, expect, it } from "vitest";
import type { AgentRunSummary, ApprovalSummary, FindingSummary, ReportSummary, SetupStatus } from "../api/types";
import { deriveZeroModules, type ZeroModuleState } from "./zeroLayerModules";

const baseState: ZeroModuleState = {
  workspaceState: "ready",
  approvals: [],
  findings: [],
  reports: [],
  assets: [],
  engagementName: "Project Aurora",
};

const approval = {
  id: "approval-1",
  runId: "run-1",
  engagementId: "project-1",
  status: "pending",
  risk: "active",
  toolName: "nmap.service_detection",
  agentName: "Network analyst",
  target: "gateway.local",
  rationale: "Confirm the exposed service.",
  expectedEffects: "Sends a bounded probe.",
  arguments: {},
  createdAt: "2026-07-14T10:00:00Z",
} satisfies ApprovalSummary;

const run = {
  id: "run-1",
  engagementId: "project-1",
  title: "Validate external services",
  status: "running",
  updatedAt: "2026-07-14T10:05:00Z",
  completedTasks: 2,
  totalTasks: 5,
} satisfies AgentRunSummary;

const finding = {
  id: "finding-1",
  engagementId: "project-1",
  title: "Exposed administration service",
  description: "A privileged service is reachable.",
  severity: "critical",
  severityRationale: "External privileged access.",
  status: "validated",
  assetIds: ["asset-1"],
  evidenceIds: ["evidence-1"],
  affectedAssetCount: 1,
  evidenceCount: 1,
  cveIds: [],
  cweIds: [],
  updatedAt: "2026-07-14T10:10:00Z",
  revision: 1,
} satisfies FindingSummary;

describe("deriveZeroModules", () => {
  it("surfaces only the three highest-priority truthful modules", () => {
    const modules = deriveZeroModules({
      ...baseState,
      approvals: [approval],
      run,
      findings: [finding],
      setupStatus: {
        core: { status: "degraded", detail: "Runner verification is limited." },
        terminal: { status: "needs_runner", candidates: [], imagePreparation: { phase: "not_started", progressIndeterminate: false, canCancel: false, canRetry: false } },
        assistant: { status: "needs_model" },
      } satisfies SetupStatus,
      reports: [{
        id: "report-1",
        engagementId: "project-1",
        title: "Assessment draft",
        status: "draft",
        executiveSummary: "",
        findingIds: [],
        observationIds: [],
        artifactIds: [],
        createdAt: "2026-07-14T09:00:00Z",
        updatedAt: "2026-07-14T09:30:00Z",
        revision: 1,
      } satisfies ReportSummary],
    });

    expect(modules.map((module) => module.id)).toEqual(["pending-approvals", "mission-run-1", "finding-finding-1"]);
    expect(modules[0].action).toEqual({ type: "activity", view: "approvals" });
    expect(modules).toHaveLength(3);
  });

  it("routes failed missions to persisted activity and ignores resolved findings", () => {
    const modules = deriveZeroModules({
      ...baseState,
      run: { ...run, status: "failed" },
      findings: [finding, { ...finding, id: "finding-2", severity: "high", status: "remediated" }],
    });

    expect(modules[0]).toMatchObject({ id: "mission-run-1", tone: "critical", action: { type: "route", to: "/?view=activity" } });
    expect(modules[1]).toMatchObject({ id: "finding-finding-1", action: { type: "route", to: "/findings" } });
    expect(modules.some((module) => module.id === "finding-finding-2")).toBe(false);
  });

  it("uses setup state and a real project summary when no work is active", () => {
    const modules = deriveZeroModules({
      ...baseState,
      setupStatus: {
        core: { status: "ready" },
        terminal: { status: "ready", candidates: [], imagePreparation: { phase: "ready", progressIndeterminate: false, canCancel: false, canRetry: false } },
        assistant: { status: "needs_model", detail: "No model is connected." },
      },
    });

    expect(modules.map((module) => module.id)).toEqual(["runtime-assistant", "project-overview"]);
    expect(modules[0]).toMatchObject({ title: "Connect an assistant model", action: { type: "route", to: "/settings" } });
    expect(modules[1]).toMatchObject({ title: "Project Aurora", action: { type: "route", to: "/project" } });
  });

  it("continues the newest non-final report through the existing Reports route", () => {
    const modules = deriveZeroModules({
      ...baseState,
      reports: [{
        id: "report-2",
        engagementId: "project-1",
        title: "Executive assessment",
        status: "review",
        executiveSummary: "",
        findingIds: ["finding-1"],
        observationIds: [],
        artifactIds: [],
        createdAt: "2026-07-14T09:00:00Z",
        updatedAt: "2026-07-14T11:30:00Z",
        revision: 2,
      }],
    });

    expect(modules[0]).toMatchObject({ id: "report-report-2", title: "Executive assessment", action: { type: "route", to: "/reports" } });
    expect(modules[1].id).toBe("project-overview");
  });

  it("falls back to the project overview without inventing work", () => {
    expect(deriveZeroModules(baseState)).toEqual([
      expect.objectContaining({ id: "project-overview", title: "Project Aurora", priority: 900 }),
    ]);
  });
});
