import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { SecurityBrowserIdentity, SecurityBrowserSession } from "../api/types";
import { BrowserResearchSuite } from "./BrowserResearchSuite";
import { DialogProvider } from "./DialogSystem";

const identity = { id: "identity-1", name: "Operator" } as SecurityBrowserIdentity;
const session = {
  id: "session-1",
  activeTabId: "tab-1",
  tabs: [{ id: "tab-1", url: "https://app.example.test/", title: "App", position: 0 }],
} as SecurityBrowserSession;

function renderSuite(element: React.ReactElement) {
  return render(<MemoryRouter><DialogProvider>{element}</DialogProvider></MemoryRouter>);
}

function api(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    getSecurityBrowserResearch: vi.fn().mockResolvedValue({
      siteNodes: [{
        id: "node-1", revision: 1, sessionId: session.id, identityId: identity.id,
        url: "https://app.example.test/api/users?page=1", method: "GET", kind: "api",
        discoverySource: "proxy", statusCode: 200, parameterNames: ["page"], evidenceIds: [],
        firstSeenAt: new Date().toISOString(), lastSeenAt: new Date().toISOString(),
      }],
      crawlJobs: [], intercepts: [], repeaterTabs: [], repeaterResults: [], attacks: [], attackResults: [], tokenAnalyses: [],
    }),
    ...overrides,
  } as unknown as ApiClient;
}

describe("BrowserResearchSuite", () => {
  it("renders the durable target map and bounded crawl controls", async () => {
    renderSuite(<BrowserResearchSuite api={api()} desktop identity={identity} operatorId="operator" projectId="project-1" session={session} view="target" />);

    expect(await screen.findByRole("heading", { name: "Target map" })).toBeInTheDocument();
    expect(screen.getByText("https://app.example.test/api/users?page=1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create bounded crawl" })).toBeEnabled();
    expect(screen.getByLabelText("Crawl start URL")).toHaveValue("https://app.example.test/");
  });

  it("keeps native crawl execution unavailable on a paired browser", async () => {
    renderSuite(<BrowserResearchSuite api={api()} desktop={false} identity={identity} operatorId="operator" projectId="project-1" session={session} view="target" />);

    expect(await screen.findByRole("button", { name: "Create bounded crawl" })).toBeDisabled();
    expect(screen.getByText(/desktop owns network execution/i)).toBeInTheDocument();
  });

  it("edits and queues a durable Repeater request while showing retained results", async () => {
    const workspace = {
      siteNodes: [], crawlJobs: [], intercepts: [], attacks: [], attackResults: [], tokenAnalyses: [],
      repeaterTabs: [{
        id: "repeater-1", revision: 2, sessionId: session.id, identityId: identity.id,
        name: "Profile", group: "Ungrouped", notes: "", protocol: "http", method: "POST",
        url: "https://app.example.test/profile", headers: [["Accept", "application/json"]], bodyTemplate: "{}",
        historyExchangeIds: ["result-1"], evidenceIds: [], state: "ready", requestCount: 1,
      }],
      repeaterResults: [{
        id: "result-1", revision: 1, tabId: "repeater-1", sequence: 0, statusCode: 200,
        responseHeaders: [["Content-Type", "application/json"]], responseBytes: 12, durationMs: 4,
        createdAt: new Date().toISOString(),
      }],
    };
    const transition = vi.fn().mockResolvedValue({ ...workspace.repeaterTabs[0], state: "queued", revision: 3 });
    const client = api({
      getSecurityBrowserResearch: vi.fn().mockResolvedValue(workspace),
      transitionSecurityBrowserRepeaterTab: transition,
    });
    renderSuite(<BrowserResearchSuite api={client} desktop identity={identity} operatorId="operator" projectId="project-1" session={session} view="repeater" />);

    expect(await screen.findByText("Profile")).toBeVisible();
    fireEvent.click(screen.getByText("Result history (1)"));
    expect(screen.getByText("200")).toBeVisible();
    fireEvent.click(screen.getByText("Profile").closest("button")!);
    expect(screen.getByLabelText("Headers JSON")).toHaveValue('{\n  "Accept": "application/json"\n}');
    fireEvent.click(screen.getByRole("button", { name: "Send once" }));
    await waitFor(() => expect(transition).toHaveBeenCalledWith(expect.objectContaining({ id: "repeater-1" }), "queue", "operator"));
  });

  it("keeps native Repeater sends disabled on a paired browser", async () => {
    const workspace = {
      siteNodes: [], crawlJobs: [], intercepts: [], attacks: [], attackResults: [], tokenAnalyses: [], repeaterResults: [],
      repeaterTabs: [{
        id: "repeater-1", revision: 1, sessionId: session.id, identityId: identity.id,
        name: "Profile", group: "Ungrouped", notes: "", protocol: "http", method: "GET",
        url: "https://app.example.test/profile", headers: [], bodyTemplate: "", historyExchangeIds: [], evidenceIds: [],
        state: "ready", requestCount: 0,
      }],
    };
    renderSuite(<BrowserResearchSuite api={api({ getSecurityBrowserResearch: vi.fn().mockResolvedValue(workspace) })} desktop={false} identity={identity} operatorId="operator" projectId="project-1" session={session} view="repeater" />);
    expect(await screen.findByRole("button", { name: "Send once" })).toBeDisabled();
    expect(screen.getByText(/paired desktop performs sends/i)).toBeVisible();
  });

  it("rejects an Intruder draft whose declared marker is absent", async () => {
    const create = vi.fn();
    renderSuite(<BrowserResearchSuite api={api({ createSecurityBrowserAttack: create })} desktop identity={identity} operatorId="operator" projectId="project-1" session={session} view="intruder" />);
    await screen.findByRole("heading", { name: "Intruder" });
    fireEvent.change(screen.getByLabelText(/URL template/), { target: { value: "https://app.example.test/static" } });
    fireEvent.click(screen.getByRole("button", { name: "Save attack draft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Add §id§");
    expect(create).not.toHaveBeenCalled();
  });

  it("maps pitchfork positions to ordered payload sets with an exact request budget", async () => {
    const create = vi.fn().mockResolvedValue({ id: "attack-1" });
    renderSuite(<BrowserResearchSuite api={api({ createSecurityBrowserAttack: create })} desktop identity={identity} operatorId="operator" projectId="project-1" session={session} view="intruder" />);
    await screen.findByRole("heading", { name: "Intruder" });
    fireEvent.change(screen.getByLabelText("Strategy"), { target: { value: "pitchfork" } });
    fireEvent.change(screen.getByLabelText(/Position names/), { target: { value: "id, role" } });
    fireEvent.change(screen.getByLabelText(/URL template/), { target: { value: "https://app.example.test/users/§id§" } });
    fireEvent.change(screen.getByLabelText("Body template"), { target: { value: '{"role":"§role§"}' } });
    fireEvent.change(screen.getByLabelText(/Payload sets/), { target: { value: "1\n2\n---\nuser\nadmin" } });
    fireEvent.click(screen.getByRole("button", { name: "Save attack draft" }));
    await waitFor(() => expect(create).toHaveBeenCalledWith("project-1", expect.objectContaining({
      strategy: "pitchfork",
      positions: ["id", "role"],
      payloadSets: [["1", "2"], ["user", "admin"]],
      maxRequests: 2,
    })));
  });
});
