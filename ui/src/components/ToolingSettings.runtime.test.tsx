import {render, screen, waitFor} from "@testing-library/react";
import {describe, expect, it, vi} from "vitest";
import type {AutomationRuntimeInfo} from "../api/types";
import {AutomationRuntimeSettings} from "./ToolingSettings";
import {startingProject} from "../test/projectRuntimeStarting";

const fixture = vi.hoisted(() => ({api: {} as Record<string, unknown>}));
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api: fixture.api, coreState: "online", previewMode: false, engagement: {id: "project-1", name: "Project one"}})}));
vi.mock("../hooks/useCredentialVault", () => ({useCredentialVault: () => ({state: "unlocked", available: true, refresh: vi.fn()}), vaultUnavailableNote: () => ""}));
vi.mock("../diagnostics", async (original) => ({...(await original<typeof import("../diagnostics")>()), logDiagnostic: vi.fn(), logCaughtDiagnostic: vi.fn(), DiagnosticErrorNotice: ({error}: {error: string}) => <div role="alert">{error}</div>}));
vi.mock("./SettingsSaveFeedback", () => ({announceSettingsSaved: vi.fn()}));

const runtime: AutomationRuntimeInfo = {configured: true, ready: true, detail: "Kali runtime ready", inventory: []};

describe("automation runtime settings while the project starts", () => {
  it("keeps checking instead of failing on the project's starting worker, then shows its Host mode", async () => {
    const {projectRuntime, unavailable} = startingProject();
    const getAutomationPolicy = vi.fn()
      .mockImplementationOnce(async () => { throw unavailable("engagements/project-1/automation-policy", "idle"); })
      .mockResolvedValue({engagementId: "project-1", revision: 1, executionMode: "host", approvalPolicy: "on_boundary", networkEnabled: false, maxTimeoutMs: 300000});
    fixture.api = {getAutomationRuntime: vi.fn().mockResolvedValue(runtime), listVpnProfiles: vi.fn().mockResolvedValue([]), getAutomationPolicy, projectRuntime};
    render(<AutomationRuntimeSettings />);
    await waitFor(() => expect(getAutomationPolicy).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Refresh"})).toBeDisabled();
    expect(await screen.findByRole("heading", {name: "Host mode"}, {timeout: 4_000})).toBeVisible();
    expect(getAutomationPolicy).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
