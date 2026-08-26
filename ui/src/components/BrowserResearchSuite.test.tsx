import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { SecurityBrowserIdentity, SecurityBrowserSession } from "../api/types";
import { BrowserResearchSuite } from "./BrowserResearchSuite";

const identity = { id: "identity-1", name: "Operator" } as SecurityBrowserIdentity;
const session = {
  id: "session-1",
  activeTabId: "tab-1",
  tabs: [{ id: "tab-1", url: "https://app.example.test/", title: "App", position: 0 }],
} as SecurityBrowserSession;

function api(): ApiClient {
  return {
    getSecurityBrowserResearch: vi.fn().mockResolvedValue({
      siteNodes: [{
        id: "node-1", revision: 1, sessionId: session.id, identityId: identity.id,
        url: "https://app.example.test/api/users?page=1", method: "GET", kind: "api",
        discoverySource: "proxy", statusCode: 200, parameterNames: ["page"], evidenceIds: [],
        firstSeenAt: new Date().toISOString(), lastSeenAt: new Date().toISOString(),
      }],
      crawlJobs: [], intercepts: [], repeaterTabs: [], attacks: [], attackResults: [], tokenAnalyses: [],
    }),
  } as unknown as ApiClient;
}

describe("BrowserResearchSuite", () => {
  it("renders the durable target map and bounded crawl controls", async () => {
    render(<MemoryRouter><BrowserResearchSuite api={api()} desktop identity={identity} operatorId="operator" projectId="project-1" session={session} view="target" /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "Target map" })).toBeInTheDocument();
    expect(screen.getByText("https://app.example.test/api/users?page=1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create bounded crawl" })).toBeEnabled();
    expect(screen.getByLabelText("Crawl start URL")).toHaveValue("https://app.example.test/");
  });

  it("keeps native crawl execution unavailable on a paired browser", async () => {
    render(<MemoryRouter><BrowserResearchSuite api={api()} desktop={false} identity={identity} operatorId="operator" projectId="project-1" session={session} view="target" /></MemoryRouter>);

    expect(await screen.findByRole("button", { name: "Create bounded crawl" })).toBeDisabled();
    expect(screen.getByText(/desktop owns network execution/i)).toBeInTheDocument();
  });
});
