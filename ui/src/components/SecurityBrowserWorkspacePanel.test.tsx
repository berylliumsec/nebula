import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type {
  SecurityBrowserAssessmentWorkspace,
  SecurityBrowserIdentity,
  SecurityBrowserSession,
} from "../api/types";
import { SecurityBrowserWorkspacePanel } from "./SecurityBrowserWorkspacePanel";

vi.mock("../api/assessmentEvents", () => ({
  AssessmentEventStream: class {
    connect() {}
    disconnect() {}
  },
}));

const identity: SecurityBrowserIdentity = {
  id: "identity-1",
  name: "Member",
  description: "Member account",
  color: "#7c6cff",
  storagePartition: "browser-00000000-0000-0000-0000-000000000001",
  ephemeral: false,
  isDefault: true,
  revision: 1,
};

const session: SecurityBrowserSession = {
  id: "session-1",
  name: "Primary session",
  identityId: identity.id,
  status: "active",
  captureMode: "headers",
  proxyEnabled: false,
  proxyTrustAcknowledged: false,
  tabs: [],
  upstreamProxyEnabled: false,
  interceptionEnabled: false,
  lastSeenAt: "2026-08-28T00:00:00Z",
  revision: 1,
};

const budget = {
  maxRequests: 2_000,
  maxActions: 500,
  maxDurationSeconds: 3_600,
  maxConcurrency: 2,
  requestsUsed: 0,
  actionsUsed: 0,
};

function workspace(): SecurityBrowserAssessmentWorkspace {
  return {
    assessments: [],
    steps: [],
    candidates: [],
    validationGrants: [],
    engines: [
      {
        adapter: "managed-chromium",
        displayName: "Managed Chromium",
        contractVersion: "1",
        state: "ready",
        installedVersion: "149.0.7827.55",
        digest: `sha256:${"a".repeat(64)}`,
        actions: ["navigate", "click"],
        protocols: ["http", "https", "websocket"],
        checkFamilies: [],
        desktopOnly: true,
      },
      {
        adapter: "zap",
        displayName: "OWASP ZAP scanner",
        contractVersion: "1",
        state: "unavailable",
        actions: [],
        protocols: [],
        checkFamilies: [],
        unavailabilityReason: "Scanner runtime is not prepared.",
        recoveryAction: "Prepare the scanner runtime.",
        desktopOnly: true,
      },
    ],
    profiles: [
      { id: "explore", name: "Explore", summary: "Manual exploration.", riskClasses: ["passive"], requiredAdapters: ["managed-chromium"], defaultBudget: budget, validationLocked: false },
      { id: "standard", name: "Standard", summary: "Crawl and passive analysis.", riskClasses: ["passive"], requiredAdapters: ["managed-chromium", "zap"], defaultBudget: budget, validationLocked: false },
      { id: "deep", name: "Deep", summary: "Bounded active checks.", riskClasses: ["passive", "active_scan"], requiredAdapters: ["managed-chromium", "zap"], defaultBudget: budget, validationLocked: false },
      { id: "api", name: "API", summary: "API import and testing.", riskClasses: ["passive", "active_scan"], requiredAdapters: ["managed-chromium", "zap"], defaultBudget: budget, validationLocked: false },
      { id: "validation", name: "Validation", summary: "Issue validation.", riskClasses: ["exploitation"], requiredAdapters: ["managed-chromium"], defaultBudget: budget, validationLocked: true },
    ],
  };
}

function api(snapshot = workspace()): ApiClient {
  return {
    baseUrl: "http://127.0.0.1:8765/api/v1",
    getToken: () => "test-token",
    getSecurityBrowserAssessments: vi.fn(async () => snapshot),
    createSecurityBrowserAssessment: vi.fn(async (_projectId, request) => ({
      id: "assessment-1",
      revision: 1,
      engagementId: "project-1",
      name: request.name,
      objective: request.objective,
      profile: request.profile,
      sessionId: request.sessionId,
      identityIds: request.identityIds,
      primaryIdentityId: request.primaryIdentityId,
      targetUrls: request.targetUrls,
      scopePolicyId: "scope-1",
      scopePolicyRevision: 4,
      riskClasses: ["passive"],
      status: "draft",
      phase: "preflight",
      progress: 0,
      budget,
      coverage: { discoveredUrls: 0, visitedUrls: 0, analyzedExchanges: 0, discoveredForms: 0, discoveredApis: 0, websocketChannels: 0 },
      engines: snapshot.engines,
      evidenceIds: [],
      candidateIds: [],
      controlOwner: "nebula",
      createdAt: "2026-08-28T00:00:00Z",
      updatedAt: "2026-08-28T00:00:00Z",
    })),
  } as unknown as ApiClient;
}

function renderPanel(client: ApiClient, desktop = true) {
  return render(
    <MemoryRouter initialEntries={["/project?view=browser&tool=traffic"]}>
      <SecurityBrowserWorkspacePanel
        api={client}
        desktop={desktop}
        projectId="project-1"
        identity={identity}
        session={session}
        targetOptions={["https://app.example.test/"]}
        toolNavigation={<nav><button type="button">Traffic</button></nav>}
        onClose={() => undefined}
      >
        <div>Traffic tool content</div>
      </SecurityBrowserWorkspacePanel>
    </MemoryRouter>,
  );
}

function assessmentWorkspace(): SecurityBrowserAssessmentWorkspace {
  const snapshot = workspace();
  snapshot.assessments = [{
    id: "assessment-1", revision: 2, engagementId: "project-1", name: "Portal review",
    objective: "Review the member portal", profile: "standard", sessionId: session.id,
    identityIds: [identity.id], primaryIdentityId: identity.id,
    targetUrls: ["https://app.example.test/"], scopePolicyId: "scope-1",
    scopePolicyRevision: 4, riskClasses: ["passive"], status: "running", phase: "passive_audit",
    progress: 0.5, budget, coverage: { discoveredUrls: 2, visitedUrls: 1, analyzedExchanges: 4, discoveredForms: 1, discoveredApis: 0, websocketChannels: 0 },
    engines: snapshot.engines, evidenceIds: [], candidateIds: ["candidate-1"], controlOwner: "nebula",
    createdAt: "2026-08-28T00:00:00Z", updatedAt: "2026-08-28T00:01:00Z",
  }];
  snapshot.candidates = [{
    id: "candidate-1", revision: 1, assessmentId: "assessment-1", ruleId: "reflected-input",
    checkFamily: "xss", title: "Reflected input", cwe: "CWE-79",
    targetUrl: "https://app.example.test/search", insertionPoint: "query:q", severity: "medium",
    confidence: "firm", evidenceIds: ["evidence-1"], validationStatus: "unvalidated",
  }];
  return snapshot;
}

describe("SecurityBrowserWorkspacePanel", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("walks a new operator through all five preflight decisions before creating", async () => {
    const client = api();
    renderPanel(client);
    await waitFor(() => expect(screen.getByRole("button", { name: /start guided test/i })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /start guided test/i }));
    expect(screen.getByText("Where may Nebula test?")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    expect(screen.getByText("Use one continuous browser identity")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    expect(screen.getByText("What outcome should the test pursue?")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    expect(screen.getByText("Choose depth and traffic risk")).toBeVisible();
    expect(screen.getByRole("button", { name: /Validation/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    expect(screen.getByText("Review before anything executes")).toBeVisible();
    expect(screen.getByText("Scanner runtime is not prepared.")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /create assessment/i }));
    await waitFor(() => expect(client.createSecurityBrowserAssessment).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({
        profile: "standard",
        targetUrls: ["https://app.example.test/"],
        primaryIdentityId: "identity-1",
      }),
    ));
  });

  it("keeps mobile as a monitor surface and prevents native execution start", async () => {
    renderPanel(api(), false);
    await waitFor(() => expect(screen.getByText(/Monitor, approve, pause, stop, and retry/)).toBeVisible());
    expect(screen.getByRole("button", { name: /start guided test/i })).toBeDisabled();
    expect(screen.getByText("Traffic tool content")).toBeVisible();
  });

  it("previews and requests an explicit target-specific validation grant", async () => {
    const snapshot = assessmentWorkspace();
    const client = api(snapshot);
    client.grantSecurityBrowserCandidateValidation = vi.fn(async (_candidate, request) => ({
      id: "grant-1", revision: 1, assessmentId: "assessment-1", candidateId: "candidate-1",
      targetUrl: "https://app.example.test/search", technique: request.technique,
      maxRequests: request.maxRequests, requestsUsed: 0, durationSeconds: request.durationSeconds,
      expiresAt: "2026-08-28T00:11:00Z", status: "active" as const,
    }));
    render(
      <MemoryRouter initialEntries={["/project?view=browser&tool=analyze&assessment=assessment-1"]}>
        <SecurityBrowserWorkspacePanel api={client} desktop projectId="project-1" identity={identity} session={session} targetOptions={["https://app.example.test/"]} toolNavigation={<nav><button type="button">Analyze</button></nav>} onClose={() => undefined}><div>Analyze content</div></SecurityBrowserWorkspacePanel>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByRole("heading", { name: "Candidate issues" })).toBeVisible());
    fireEvent.click(screen.getByRole("button", { name: /Reflected input/ }));
    expect(screen.getByText("This authorizes exploit-validation traffic")).toBeVisible();
    expect(screen.getByRole("button", { name: /grant bounded validation/i })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Exact validation technique"), {
      target: { value: "Replay the reflected query with one inert marker and a negative encoding control." },
    });
    expect(screen.getByText(/Up to 10 requests over 600 seconds/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /grant bounded validation/i }));
    await waitFor(() => expect(client.grantSecurityBrowserCandidateValidation).toHaveBeenCalledWith(
      expect.objectContaining({ id: "candidate-1" }),
      {
        technique: "Replay the reflected query with one inert marker and a negative encoding control.",
        maxRequests: 10,
        durationSeconds: 600,
      },
    ));
  });
});
