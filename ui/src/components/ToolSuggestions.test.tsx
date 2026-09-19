import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import {beforeEach, describe, expect, it, vi} from "vitest";
import {MemoryRouter} from "react-router-dom";
import {DialogProvider} from "./DialogSystem";
import {ToolSuggestionChip, toolSuggestionLabel} from "./ToolSuggestionChip";
import {TypeSafeIntegrationSettings} from "./TypeSafeIntegrationSettings";
import {EngagementPolicySettings} from "./EngagementPolicySettings";
import {mapToolSuggestions} from "../api/client";
import type {ToolSuggestionSummary, TypeSafeIntegration} from "../api/types";

const fixture = vi.hoisted(() => ({api: {} as Record<string, ReturnType<typeof vi.fn>>}));
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api: fixture.api, coreState: "online", engagement: {id: "project"}, previewMode: false})}));

const summary = (overrides: Partial<ToolSuggestionSummary> = {}): ToolSuggestionSummary => ({
  status: "suggested",
  preloaded: ["mcp.tracker.search_issues"],
  suggested: ["mcp.tracker.get_issue", "mcp.tracker.link_issues"],
  used: ["mcp.tracker.get_issue"],
  loadedByModel: [],
  unloadedCount: 38,
  onDemandCount: 41,
  model: "jev-1.13.0",
  latencyMs: 412,
  ...overrides,
});

describe("tool suggestion chip", () => {
  it("summarizes the turn and lists plain statuses when expanded", () => {
    render(<ToolSuggestionChip summary={summary()} />);
    const toggle = screen.getByText("3 tools suggested · 1 preloaded");
    fireEvent.click(toggle);
    const rows = screen.getAllByRole("listitem").map((row) => row.textContent);
    expect(rows).toEqual([
      "mcp.tracker.search_issuesPreloaded",
      "mcp.tracker.get_issueSuggested · used",
      "mcp.tracker.link_issuesSuggested · not used",
    ]);
    expect(screen.getByText("38 more on-demand tools stayed unloaded · Jev 1.13.0 · 412 ms")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/0\.\d+|%/);
  });

  it("explains an unavailable turn without asking for action", () => {
    const failed = summary({status: "unavailable", preloaded: [], suggested: [], used: [], error: "ConnectTimeout: timed out"});
    expect(toolSuggestionLabel(failed)).toBe("No suggestions · Jev timed out");
    render(<ToolSuggestionChip summary={failed} />);
    expect(screen.getByText(/41 on-demand tools stayed available through the catalog/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("maps only well-formed Core summaries", () => {
    expect(mapToolSuggestions({status: "bogus"})).toBeUndefined();
    expect(mapToolSuggestions(null)).toBeUndefined();
    expect(mapToolSuggestions({status: "no_tool_needed", preloaded: ["a", 3], unloaded_count: 2, on_demand_count: 2, probabilities: {a: 0.9}}))
      .toEqual({status: "no_tool_needed", preloaded: ["a"], suggested: [], used: [], loadedByModel: [], unloadedCount: 2, onDemandCount: 2, model: undefined, latencyMs: undefined, error: undefined});
  });
});

const integration = (overrides: Partial<TypeSafeIntegration> = {}): TypeSafeIntegration => ({available: false, vaultAvailable: true, vaultState: "available", projectsUsing: 0, ...overrides});
const working = integration({source: "vault", available: true, projectsUsing: 3, lastTest: {testedAt: new Date().toISOString(), ok: true, latencyMs: 412, model: "jev-1.13.0"}});

describe("TypeSafe key settings", () => {
  beforeEach(() => {
    fixture.api = {
      getTypeSafeIntegration: vi.fn(async () => integration()),
      saveTypeSafeKey: vi.fn(async () => working),
      testTypeSafeKey: vi.fn(async () => working),
      removeTypeSafeKey: vi.fn(async () => integration()),
    };
  });
  const view = () => render(<DialogProvider><TypeSafeIntegrationSettings /></DialogProvider>);

  it("saves a new key to the vault, tests it and never shows it again", async () => {
    view();
    expect(await screen.findByText("Not configured")).toBeInTheDocument();
    const input = screen.getByLabelText("TypeSafe API key");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.change(input, {target: {value: "ts-secret"}});
    fireEvent.click(screen.getByRole("button", {name: "Save and test"}));
    await waitFor(() => expect(fixture.api.saveTypeSafeKey).toHaveBeenCalledWith("ts-secret", "vault"));
    expect(await screen.findByText("Working")).toBeInTheDocument();
    expect(screen.getByText("Saved in OS keychain")).toBeInTheDocument();
    expect(screen.getByText("3 projects")).toBeInTheDocument();
    expect(screen.queryByLabelText("TypeSafe API key")).toBeNull();
    expect(document.body.textContent).not.toContain("ts-secret");
  });

  it("names the remedy and keeps the key for the session when the host vault is locked", async () => {
    fixture.api.getTypeSafeIntegration.mockResolvedValue(integration({vaultAvailable: false, vaultState: "locked"}));
    view();
    expect(await screen.findByText(/credential vault is locked/)).toBeInTheDocument();
    expect(screen.getByText(/Unlock the keyring on the Core host/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("TypeSafe API key"), {target: {value: "k"}});
    fireEvent.click(screen.getByRole("button", {name: "Save and test"}));
    await waitFor(() => expect(fixture.api.saveTypeSafeKey).toHaveBeenCalledWith("k", "session"));
  });

  it("keeps the key for the session when the vault is unavailable", async () => {
    fixture.api.getTypeSafeIntegration.mockResolvedValue(integration({vaultAvailable: false, vaultState: "unavailable"}));
    view();
    expect(await screen.findByText(/credential vault is unavailable/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("TypeSafe API key"), {target: {value: "k"}});
    fireEvent.click(screen.getByRole("button", {name: "Save and test"}));
    await waitFor(() => expect(fixture.api.saveTypeSafeKey).toHaveBeenCalledWith("k", "session"));
  });

  it("tests and removes a saved key after confirmation", async () => {
    fixture.api.getTypeSafeIntegration.mockResolvedValue(working);
    view();
    fireEvent.click(await screen.findByRole("button", {name: "Test"}));
    await waitFor(() => expect(fixture.api.testTypeSafeKey).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", {name: "Remove"}));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/3 projects use it/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", {name: "Remove key"}));
    await waitFor(() => expect(fixture.api.removeTypeSafeKey).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Not configured")).toBeInTheDocument();
  });

  it("reports a failed test", async () => {
    fixture.api.getTypeSafeIntegration.mockResolvedValue(integration({source: "environment", available: true, lastTest: {testedAt: new Date().toISOString(), ok: false, error: "ConnectError: refused"}}));
    view();
    expect(await screen.findByText("Not working")).toBeInTheDocument();
    expect(screen.getByText(/ConnectError: refused/)).toBeInTheDocument();
    expect(screen.getByText("TYPESAFE_API_KEY from Core's environment")).toBeInTheDocument();
  });
});

describe("project opt-in", () => {
  const scope = (overrides: Record<string, unknown> = {}) => ({engagementId: "project", revision: 1, allowedCidrs: [], allowedDomains: [], allowedUrls: [], allowedPorts: [], prohibitedActions: [], localOnly: false, toolSuggestions: false, maxConcurrency: 1, allowAllTargets: false, grants: [], ...overrides});
  const setup = (scopeValue: ReturnType<typeof scope>, status: TypeSafeIntegration) => {
    fixture.api = {
      getEngagementScope: vi.fn(async () => scopeValue),
      getAutomationPolicy: vi.fn(async () => ({engagementId: "project", revision: 1, executionMode: "docker", approvalPolicy: "on_boundary", networkEnabled: false, maxTimeoutMs: 300000})),
      listVpnProfiles: vi.fn(async () => []),
      getTypeSafeIntegration: vi.fn(async () => status),
      updateEngagementScope: vi.fn(async (_id, body) => ({...scopeValue, ...body, revision: 2})),
    };
    render(<MemoryRouter><DialogProvider><EngagementPolicySettings /></DialogProvider></MemoryRouter>);
    return screen.findByRole("checkbox", {name: /Suggest tools with TypeSafe Jev/});
  };

  it("can be turned on with a working key and is saved with the scope", async () => {
    const option = await setup(scope(), working);
    await waitFor(() => expect(option).toBeEnabled());
    fireEvent.click(option);
    fireEvent.click(screen.getByRole("button", {name: "Save scope"}));
    await waitFor(() => expect(fixture.api.updateEngagementScope).toHaveBeenCalledWith("project", expect.objectContaining({toolSuggestions: true})));
  });

  it("is disabled with a link to Integrations when no key works", async () => {
    const option = await setup(scope(), integration());
    await waitFor(() => expect(fixture.api.getTypeSafeIntegration).toHaveBeenCalled());
    expect(option).toBeDisabled();
    expect(screen.getByRole("link", {name: /Add a key in Settings › Integrations/})).toHaveAttribute("href", "#typesafe-integration-settings");
  });

  it("is unavailable while Local only is on", async () => {
    const option = await setup(scope({localOnly: true, toolSuggestions: true}), working);
    await waitFor(() => expect(fixture.api.getTypeSafeIntegration).toHaveBeenCalled());
    expect(option).toBeDisabled();
    expect(option).not.toBeChecked();
    expect(option).toHaveAccessibleDescription("Unavailable while Local only is on.");
  });

  it("warns when it is on but the key stopped working, and can still be turned off", async () => {
    const option = await setup(scope({toolSuggestions: true}), integration({source: "vault", available: true, lastTest: {testedAt: new Date().toISOString(), ok: false}}));
    expect(await screen.findByText(/no working TypeSafe key is available/)).toBeInTheDocument();
    expect(option).toBeEnabled();
    fireEvent.click(option);
    expect(option).not.toBeChecked();
  });
});

describe("key changes reach the project opt-in", () => {
  it("enables the option when a key is saved elsewhere on the page", async () => {
    const {TYPESAFE_CHANGED_EVENT} = await import("./TypeSafeIntegrationSettings");
    fixture.api = {
      getEngagementScope: vi.fn(async () => ({engagementId: "project", revision: 1, allowedCidrs: [], allowedDomains: [], allowedUrls: [], allowedPorts: [], prohibitedActions: [], localOnly: false, toolSuggestions: false, maxConcurrency: 1, allowAllTargets: false, grants: []})),
      getAutomationPolicy: vi.fn(async () => ({engagementId: "project", revision: 1, executionMode: "docker", approvalPolicy: "on_boundary", networkEnabled: false, maxTimeoutMs: 300000})),
      listVpnProfiles: vi.fn(async () => []),
      getTypeSafeIntegration: vi.fn(async () => integration()),
    };
    render(<MemoryRouter><DialogProvider><EngagementPolicySettings /></DialogProvider></MemoryRouter>);
    const option = await screen.findByRole("checkbox", {name: /Suggest tools with TypeSafe Jev/});
    await waitFor(() => expect(fixture.api.getTypeSafeIntegration).toHaveBeenCalled());
    expect(option).toBeDisabled();
    window.dispatchEvent(new CustomEvent(TYPESAFE_CHANGED_EVENT, {detail: working}));
    await waitFor(() => expect(option).toBeEnabled());
  });
});
