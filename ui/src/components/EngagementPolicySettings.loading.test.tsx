import {act, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {beforeEach, describe, expect, it, vi} from "vitest";
import {MemoryRouter} from "react-router-dom";
import {DialogProvider} from "./DialogSystem";
import {EngagementPolicySettings} from "./EngagementPolicySettings";
import {ApiError} from "../api/client";

const fixture = vi.hoisted(() => ({api: {} as Record<string, ReturnType<typeof vi.fn>>, project: "first"}));
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api: fixture.api, coreState: "online", engagement: {id: fixture.project}, previewMode: false})}));
const policy = (id: string, approvalPolicy = "on_boundary") => ({engagementId: id, revision: 1, executionMode: "docker", approvalPolicy, networkEnabled: false, maxTimeoutMs: 300000});
const scope = (id: string) => ({engagementId: id, revision: 1, allowedCidrs: [], allowedDomains: [`${id}.test`], allowedUrls: [], allowedPorts: [], prohibitedActions: [], localOnly: true, alwaysLoadedTools: [], maxConcurrency: 1, allowAllTargets: false, grants: []});
function view() { return <MemoryRouter><DialogProvider><EngagementPolicySettings /></DialogProvider></MemoryRouter>; }
beforeEach(() => {
  fixture.project = "first";
  fixture.api = {getEngagementScope: vi.fn(async id => scope(id)), getAutomationPolicy: vi.fn(async id => policy(id)), listVpnProfiles: vi.fn(async () => []), listScopeToolCandidates: vi.fn(async () => []), getTypeSafeIntegration: vi.fn(async () => ({available: false, vaultAvailable: true, vaultState: "available" as const, projectsUsing: 0})), getWebSearchRuntime: vi.fn(async () => ({runtimeAvailable: false, runtimeDetail: "", containerState: "absent" as const, engines: [], projectsUsing: 0}))};
});

describe("project policy hydration", () => {
  it("keeps one-time Host consent and exposes package downloads in Project settings", async () => {
    fixture.api.getAutomationPolicy.mockResolvedValueOnce({...policy("first"), executionMode: "host", hostAccessAcknowledged: true});
    fixture.api.updateAutomationPolicy = vi.fn(async (id: string, request: {networkEnabled: boolean}) => ({...policy(id), executionMode: "host", hostAccessAcknowledged: true, networkEnabled: request.networkEnabled, revision: 2}));
    render(view());
    const mode = await screen.findByRole("combobox", {name: "Project execution mode"});
    await waitFor(() => expect(mode).toHaveValue("host"));
    const consent = screen.getByRole("checkbox", {name: /Allow host filesystem and network access/});
    expect(consent).toBeChecked();
    fireEvent.change(mode, {target: {value: "docker"}});
    fireEvent.change(mode, {target: {value: "host"}});
    expect(consent).toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", {name: /Allow notebook package downloads/}));
    fireEvent.click(screen.getByRole("button", {name: "Save runtime policy"}));
    await waitFor(() => expect(fixture.api.updateAutomationPolicy).toHaveBeenCalledWith("first", expect.objectContaining({executionMode: "host", hostAccessAcknowledged: true, networkEnabled: true})));
  });

  it("offers an interrupt action for a running notebook before changing mode", async () => {
    const active = {id: "run-1", engagement_id: "first", notebook_path: "quick.ipynb", cell_id: "cell", status: "running", detail: "", revision: 2};
    fixture.api.getAutomationPolicy.mockResolvedValueOnce({...policy("first"), executionMode: "host", hostAccessAcknowledged: true});
    fixture.api.updateAutomationPolicy = vi.fn()
      .mockRejectedValueOnce(new ApiError("A notebook cell is running; interrupt it before changing project mode", 409))
      .mockResolvedValueOnce({...policy("first"), executionMode: "docker", hostAccessAcknowledged: true, revision: 2});
    fixture.api.listActiveNotebookRuns = vi.fn(async () => [active]);
    fixture.api.interruptNotebookRun = vi.fn(async () => ({...active, status: "interrupted"}));
    fixture.api.waitNotebookRun = vi.fn(async () => ({run: {...active, status: "interrupted"}, events: []}));
    render(view());
    const mode = await screen.findByRole("combobox", {name: "Project execution mode"});
    await waitFor(() => expect(mode).toHaveValue("host"));
    fireEvent.change(mode, {target: {value: "docker"}});
    fireEvent.click(screen.getByRole("button", {name: "Save runtime policy"}));
    const interrupt = await screen.findByRole("button", {name: "Interrupt notebook cell"});
    fireEvent.click(interrupt);
    await waitFor(() => expect(fixture.api.interruptNotebookRun).toHaveBeenCalledWith("first", "run-1"));
    await waitFor(() => expect(screen.getByText(/Notebook cells interrupted/)).toBeVisible());
    fireEvent.click(screen.getByRole("button", {name: "Save runtime policy"}));
    await waitFor(() => expect(fixture.api.updateAutomationPolicy).toHaveBeenCalledTimes(2));
  });

  it("disables all policy and scope inputs until their saved state arrives", async () => {
    let resolve!: (value: ReturnType<typeof policy>) => void;
    fixture.api.getAutomationPolicy.mockImplementation(() => new Promise(done => { resolve = done; }));
    render(view());
    const approval = screen.getByRole("combobox", {name: "Approval policy"});
    expect(approval).toBeDisabled();
    expect(screen.getByRole("textbox", {name: /^Allowed domains/})).toBeDisabled();
    await act(async () => { resolve(policy("first")); });
    expect(approval).toBeEnabled();
    expect(approval).toHaveValue("on_boundary");
    expect(approval).toHaveAccessibleDescription(/provider and harness tool approvals/);
  });

  it("ignores an earlier project's policy response after switching projects", async () => {
    let resolveFirst!: (value: ReturnType<typeof policy>) => void;
    fixture.api.getAutomationPolicy.mockImplementation(id => id === "first" ? new Promise(done => { resolveFirst = done; }) : Promise.resolve(policy(id, "always")));
    const {rerender} = render(view());
    fixture.project = "second";
    rerender(view());
    const approval = screen.getByRole("combobox", {name: "Approval policy"});
    await waitFor(() => expect(approval).toHaveValue("always"));
    await act(async () => { resolveFirst(policy("first")); });
    expect(approval).toHaveValue("always");
    expect(approval).toBeEnabled();
  });

  it("keeps failed initial state disabled and offers an in-place retry", async () => {
    fixture.api.getAutomationPolicy.mockRejectedValueOnce(new Error("Synthetic policy load failure"));
    render(view());
    const retry = await screen.findByRole("button", {name: "Retry loading project policy"});
    expect(screen.getByRole("combobox", {name: "Approval policy"})).toBeDisabled();
    fireEvent.click(retry);
    await waitFor(() => expect(screen.getByRole("combobox", {name: "Approval policy"})).toBeEnabled());
    expect(fixture.api.getAutomationPolicy).toHaveBeenCalledTimes(2);
  });

  it("keeps a cleared timeout field empty, refuses to save it, and saves the retyped value", async () => {
    fixture.api.updateAutomationPolicy = vi.fn(async (id: string, request: {maxTimeoutMs: number}) => ({...policy(id), maxTimeoutMs: request.maxTimeoutMs, revision: 2}));
    render(view());
    const timeout = screen.getByLabelText("Maximum command timeout (milliseconds)");
    await waitFor(() => expect(timeout).toBeEnabled());
    expect(timeout).toHaveValue(300000);

    fireEvent.change(timeout, {target: {value: ""}});
    expect(timeout).toHaveValue(null);
    fireEvent.click(screen.getByRole("button", {name: "Save runtime policy"}));
    expect(await screen.findByText(/Maximum command timeout must be a whole number/)).toBeVisible();
    expect(fixture.api.updateAutomationPolicy).not.toHaveBeenCalled();

    fireEvent.change(timeout, {target: {value: "60000"}});
    expect(timeout).toHaveValue(60000);
    fireEvent.click(screen.getByRole("button", {name: "Save runtime policy"}));
    await waitFor(() => expect(fixture.api.updateAutomationPolicy).toHaveBeenCalledWith("first", expect.objectContaining({maxTimeoutMs: 60000})));
    await waitFor(() => expect(timeout).toHaveValue(60000));
  });

  it("keeps a cleared concurrency field empty and refuses to save it", async () => {
    fixture.api.updateEngagementScope = vi.fn(async (id: string) => scope(id));
    render(view());
    const concurrency = screen.getByLabelText("Maximum concurrency");
    await waitFor(() => expect(concurrency).toBeEnabled());
    fireEvent.change(concurrency, {target: {value: ""}});
    expect(concurrency).toHaveValue(null);
    fireEvent.click(screen.getByRole("button", {name: "Save scope"}));
    expect(await screen.findByText(/Maximum concurrency must be a whole number/)).toBeVisible();
    expect(fixture.api.updateEngagementScope).not.toHaveBeenCalled();
    fireEvent.change(concurrency, {target: {value: "4"}});
    fireEvent.click(screen.getByRole("button", {name: "Save scope"}));
    await waitFor(() => expect(fixture.api.updateEngagementScope).toHaveBeenCalledWith("first", expect.objectContaining({maxConcurrency: 4})));
  });

  it.each(["runtime", "scope"])("keeps a late %s save response out of a newly selected project", async area => {
    let resolve!: (value: unknown) => void;
    fixture.api.getAutomationPolicy.mockImplementation(async id => policy(id, id === "second" ? "always" : "on_boundary"));
    const method = area === "runtime" ? "updateAutomationPolicy" : "updateEngagementScope";
    fixture.api[method] = vi.fn(() => new Promise(done => { resolve = done; }));
    const {rerender} = render(view());
    const save = screen.getByRole("button", {name: area === "runtime" ? "Save runtime policy" : "Save scope"});
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);
    await waitFor(() => expect(fixture.api[method]).toHaveBeenCalledTimes(1));
    fixture.project = "second";
    rerender(view());
    const approval = screen.getByRole("combobox", {name: "Approval policy"});
    await waitFor(() => expect(approval).toHaveValue("always"));
    await act(async () => { resolve(area === "runtime" ? policy("first") : scope("first")); });
    expect(approval).toHaveValue("always");
    expect(screen.getByRole("textbox", {name: /^Allowed domains/})).toHaveValue("second.test");
    expect(approval).toBeEnabled();
  });
});
