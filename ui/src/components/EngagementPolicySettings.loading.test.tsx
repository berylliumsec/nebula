import {act, fireEvent, render, screen, waitFor} from "@testing-library/react";
import {beforeEach, describe, expect, it, vi} from "vitest";
import {MemoryRouter} from "react-router-dom";
import {DialogProvider} from "./DialogSystem";
import {EngagementPolicySettings} from "./EngagementPolicySettings";

const fixture = vi.hoisted(() => ({api: {} as Record<string, ReturnType<typeof vi.fn>>, project: "first"}));
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api: fixture.api, coreState: "online", engagement: {id: fixture.project}, previewMode: false})}));
const policy = (id: string, approvalPolicy = "on_boundary") => ({engagementId: id, revision: 1, executionMode: "docker", approvalPolicy, networkEnabled: false, maxTimeoutMs: 300000});
const scope = (id: string) => ({engagementId: id, revision: 1, allowedCidrs: [], allowedDomains: [`${id}.test`], allowedUrls: [], allowedPorts: [], prohibitedActions: [], localOnly: true, maxConcurrency: 1, allowAllTargets: false, grants: []});
function view() { return <MemoryRouter><DialogProvider><EngagementPolicySettings /></DialogProvider></MemoryRouter>; }
beforeEach(() => {
  fixture.project = "first";
  fixture.api = {getEngagementScope: vi.fn(async id => scope(id)), getAutomationPolicy: vi.fn(async id => policy(id)), listVpnProfiles: vi.fn(async () => [])};
});

describe("project policy hydration", () => {
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
    expect(approval).toHaveAccessibleDescription(/Harness, MCP and browser permissions are separate/);
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
