import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {MemoryRouter} from "react-router-dom";
import {beforeEach, describe, expect, it, vi} from "vitest";
import {DialogProvider} from "./DialogSystem";
import {EngagementPolicySettings} from "./EngagementPolicySettings";

const fixture = vi.hoisted(() => ({api: {} as Record<string, ReturnType<typeof vi.fn>>, project: "first"}));
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api: fixture.api, coreState: "online", engagement: {id: fixture.project}, previewMode: false})}));

const policy = () => ({engagementId: "first", revision: 1, executionMode: "docker", approvalPolicy: "on_boundary", networkEnabled: false, maxTimeoutMs: 300000});
const scope = (changes: Record<string, unknown> = {}) => ({
  engagementId: "first", revision: 1, allowedCidrs: [], allowedDomains: ["acme-client.com"], allowedUrls: [],
  allowedPorts: [], prohibitedActions: [], localOnly: false, toolSuggestions: false,
  webSearch: false, webSearchDisclosesScope: false, alwaysLoadedTools: [], maxConcurrency: 1,
  allowAllTargets: false, grants: [], ...changes,
});
const runtime = (changes: Record<string, unknown> = {}) => ({
  runtimeAvailable: true, runtimeDetail: "", containerState: "ready", imageDigest: `sha256:${"a".repeat(64)}`,
  port: 24800, engines: ["duckduckgo"], projectsUsing: 1, ...changes,
});

function view() { return <MemoryRouter><DialogProvider><EngagementPolicySettings /></DialogProvider></MemoryRouter>; }

beforeEach(() => {
  fixture.api = {
    getEngagementScope: vi.fn(async () => scope()),
    updateEngagementScope: vi.fn(async (_id: string, body: Record<string, unknown>) => ({...scope(), ...body, revision: 2})),
    getAutomationPolicy: vi.fn(async () => policy()),
    listVpnProfiles: vi.fn(async () => []),
    listScopeToolCandidates: vi.fn(async () => []),
    getTypeSafeIntegration: vi.fn(async () => ({available: false, vaultAvailable: true, vaultState: "available" as const, projectsUsing: 0})),
    getWebSearchRuntime: vi.fn(async () => runtime()),
  };
});

describe("web search opt-in on a project", () => {
  it("is off by default and hides the disclosure option until it is on", async () => {
    render(view());
    const toggle = await screen.findByRole("checkbox", {name: /Web search/});
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).not.toBeChecked();
    expect(screen.queryByRole("checkbox", {name: /Allow queries naming in-scope targets/})).not.toBeInTheDocument();
  });

  it("reveals the disclosure option once web search is enabled", async () => {
    const user = userEvent.setup();
    render(view());
    const toggle = await screen.findByRole("checkbox", {name: /Web search/});
    await waitFor(() => expect(toggle).toBeEnabled());
    await user.click(toggle);
    const disclosure = screen.getByRole("checkbox", {name: /Allow queries naming in-scope targets/});
    expect(disclosure).not.toBeChecked();
    expect(disclosure).toHaveAccessibleDescription(/refused before it leaves this machine/);
  });

  it("explains that a local-only project cannot search and keeps the toggle off", async () => {
    fixture.api.getEngagementScope.mockResolvedValue(scope({localOnly: true, webSearch: true}));
    render(view());
    const toggle = await screen.findByRole("checkbox", {name: /Web search/});
    await waitFor(() => expect(fixture.api.getEngagementScope).toHaveBeenCalled());
    expect(toggle).toBeDisabled();
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription(/every search query leaves this machine/);
  });

  it("saves both fields together", async () => {
    const user = userEvent.setup();
    render(view());
    const toggle = await screen.findByRole("checkbox", {name: /Web search/});
    await waitFor(() => expect(toggle).toBeEnabled());
    await user.click(toggle);
    await user.click(screen.getByRole("checkbox", {name: /Allow queries naming in-scope targets/}));
    await user.click(screen.getByRole("button", {name: /Save scope/i}));
    await waitFor(() => expect(fixture.api.updateEngagementScope).toHaveBeenCalled());
    const body = fixture.api.updateEngagementScope.mock.calls[0][1];
    expect(body.webSearch).toBe(true);
    expect(body.webSearchDisclosesScope).toBe(true);
  });

  it("never leaves disclosure armed on a project that turned web search off", async () => {
    fixture.api.getEngagementScope.mockResolvedValue(scope({webSearch: true, webSearchDisclosesScope: true}));
    const user = userEvent.setup();
    render(view());
    const toggle = await screen.findByRole("checkbox", {name: /Web search/});
    await waitFor(() => expect(toggle).toBeChecked());
    await user.click(toggle);
    await user.click(screen.getByRole("button", {name: /Save scope/i}));
    await waitFor(() => expect(fixture.api.updateEngagementScope).toHaveBeenCalled());
    const body = fixture.api.updateEngagementScope.mock.calls[0][1];
    expect(body.webSearch).toBe(false);
    expect(body.webSearchDisclosesScope).toBe(false);
  });

  it("warns when the project opted in but the runtime is not ready", async () => {
    fixture.api.getEngagementScope.mockResolvedValue(scope({webSearch: true}));
    fixture.api.getWebSearchRuntime.mockResolvedValue(runtime({containerState: "stopped"}));
    render(view());
    await screen.findByRole("checkbox", {name: /Web search/});
    expect(await screen.findByText(/search runtime is not ready \(stopped\)/)).toBeInTheDocument();
  });

  it("links to the runtime settings when nothing is installed yet", async () => {
    fixture.api.getEngagementScope.mockResolvedValue(scope({webSearch: true}));
    fixture.api.getWebSearchRuntime.mockResolvedValue(runtime({imageDigest: undefined, containerState: "absent"}));
    render(view());
    await screen.findByRole("checkbox", {name: /Web search/});
    const link = await screen.findByRole("link", {name: /Install the search runtime/});
    expect(link).toHaveAttribute("href", "#web-search-runtime-settings");
  });
});
