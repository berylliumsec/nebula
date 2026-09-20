import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import {beforeEach, describe, expect, it, vi} from "vitest";
import {MemoryRouter} from "react-router-dom";
import {DialogProvider} from "./DialogSystem";
import {EngagementPolicySettings} from "./EngagementPolicySettings";
import {ApiError} from "../api/client";

const fixture = vi.hoisted(() => ({api: {} as Record<string, ReturnType<typeof vi.fn>>}));
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api: fixture.api, coreState: "online", engagement: {id: "project"}, previewMode: false})}));

const scope = (alwaysLoadedTools: string[] = []) => ({engagementId: "project", revision: 1, allowedCidrs: [], allowedDomains: [], allowedUrls: [], allowedPorts: [], prohibitedActions: [], localOnly: true, toolSuggestions: false, alwaysLoadedTools, maxConcurrency: 1, allowAllTargets: false, grants: []});
const candidate = (name: string, toolName: string, serverName = "tracker") => ({name, serverId: "mcp-1", serverName, toolName, description: `${toolName} description`});

function setup(scopeValue: ReturnType<typeof scope>, candidates: unknown[] | Error) {
  fixture.api = {
    getEngagementScope: vi.fn(async () => scopeValue),
    getAutomationPolicy: vi.fn(async () => ({engagementId: "project", revision: 1, executionMode: "docker", approvalPolicy: "on_boundary", networkEnabled: false, maxTimeoutMs: 300000})),
    listVpnProfiles: vi.fn(async () => []),
    getTypeSafeIntegration: vi.fn(async () => ({available: false, vaultAvailable: true, projectsUsing: 0})),
    listScopeToolCandidates: candidates instanceof Error ? vi.fn(async () => { throw candidates; }) : vi.fn(async () => candidates),
    updateEngagementScope: vi.fn(async (_id, body) => ({...scopeValue, ...body, revision: 2})),
  };
  render(<MemoryRouter><DialogProvider><EngagementPolicySettings /></DialogProvider></MemoryRouter>);
  return screen.findByRole("group", {name: "Always loaded tools"});
}

beforeEach(() => { fixture.api = {}; });

describe("always loaded tools", () => {
  it("pins a connected-source tool and saves it with the scope", async () => {
    const group = await setup(scope(), [candidate("mcp.aaaaaaaaaaaa.search", "search"), candidate("mcp.bbbbbbbbbbbb.create-issue", "create-issue")]);
    const option = await within(group).findByRole("checkbox", {name: /search/});
    expect(option).not.toBeChecked();
    expect(screen.getByText(/0 of 2 tools pinned/)).toBeInTheDocument();
    fireEvent.click(option);
    expect(screen.getByText(/1 of 2 tools pinned/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Save scope"}));
    await waitFor(() => expect(fixture.api.updateEngagementScope).toHaveBeenCalledWith("project", expect.objectContaining({alwaysLoadedTools: ["mcp.aaaaaaaaaaaa.search"]})));
  });

  it("unpins a saved tool without dropping the others", async () => {
    const group = await setup(scope(["mcp.aaaaaaaaaaaa.search", "mcp.bbbbbbbbbbbb.create-issue"]), [candidate("mcp.aaaaaaaaaaaa.search", "search"), candidate("mcp.bbbbbbbbbbbb.create-issue", "create-issue")]);
    const option = await within(group).findByRole("checkbox", {name: /create-issue/});
    await waitFor(() => expect(option).toBeChecked());
    fireEvent.click(option);
    fireEvent.click(screen.getByRole("button", {name: "Save scope"}));
    await waitFor(() => expect(fixture.api.updateEngagementScope).toHaveBeenCalledWith("project", expect.objectContaining({alwaysLoadedTools: ["mcp.aaaaaaaaaaaa.search"]})));
  });

  it("keeps a pinned tool no server offers any more until it is unpinned", async () => {
    const group = await setup(scope(["mcp.cccccccccccc.retired"]), [candidate("mcp.aaaaaaaaaaaa.search", "search")]);
    const retired = await within(group).findByRole("checkbox", {name: /mcp.cccccccccccc.retired/});
    expect(retired).toBeChecked();
    expect(within(group).getByText(/no enabled, probed MCP server offers it now/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {name: "Save scope"}));
    await waitFor(() => expect(fixture.api.updateEngagementScope).toHaveBeenCalledWith("project", expect.objectContaining({alwaysLoadedTools: ["mcp.cccccccccccc.retired"]})));
    fireEvent.click(retired);
    fireEvent.click(screen.getByRole("button", {name: "Save scope"}));
    await waitFor(() => expect(fixture.api.updateEngagementScope).toHaveBeenLastCalledWith("project", expect.objectContaining({alwaysLoadedTools: []})));
  });

  it("points at MCP settings when no Core tools are available", async () => {
    const group = await setup(scope(), new ApiError("not found", 404));
    expect(await within(group).findByText(/Add an MCP server and probe it/)).toBeInTheDocument();
    expect(screen.getByRole("link", {name: /Add an MCP server in Settings › MCP servers/})).toHaveAttribute("href", "#mcp-settings");
    expect(screen.getByRole("button", {name: "Save scope"})).toBeEnabled();
  });
});
