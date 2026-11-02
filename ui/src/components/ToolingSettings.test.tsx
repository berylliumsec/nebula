import {act, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {beforeEach, describe, expect, it, vi} from "vitest";
import type {AutomationRuntimeInfo, RunnerProfile, VpnProfile} from "../api/types";
import {AutomationRuntimeSettings, RunnerSettings} from "./ToolingSettings";
import {notifyRuntimeSettingsChanged} from "../api/runtimeSettingsEvents";

const api = {
  listRunnerProfiles: vi.fn(), updateRunnerProfile: vi.fn(),
  getAutomationRuntime: vi.fn(), listVpnProfiles: vi.fn(), getAutomationPolicy: vi.fn(),
  deleteVpnProfile: vi.fn(), createVpnProfile: vi.fn(), prepareAutomationRuntime: vi.fn(), updateAutomationPolicy: vi.fn(),
};
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api, coreState: "online", health: undefined, previewMode: false, runtime: undefined, engagement: undefined})}));
vi.mock("../hooks/useCredentialVault", () => ({useCredentialVault: () => ({state: "unlocked", available: true, refresh: vi.fn()}), vaultUnavailableNote: () => ""}));
vi.mock("../diagnostics", () => ({logCaughtDiagnostic: vi.fn(), DiagnosticErrorNotice: ({error}: {error: string}) => <div role="alert">{error}</div>}));
vi.mock("./SettingsSaveFeedback", () => ({announceSettingsSaved: vi.fn()}));

function runner(id: string, name: string): RunnerProfile {
  return {id, name, runtimeType: "podman", executable: "/usr/bin/podman", platform: "linux/arm64", isolationMode: "rootless", state: "ready", revision: 1};
}

function vpn(id: string, name: string): VpnProfile {
  return {id, name, filename: `${name}.ovpn`, remoteHost: "vpn.example", remotePort: 1194, protocol: "udp", fingerprint: "ab:cd", requiresCredentials: false, available: true, revision: 1};
}

const runtime: AutomationRuntimeInfo = {configured: true, ready: true, detail: "Kali runtime ready", inventory: []};

describe("runner settings", () => {
  beforeEach(() => vi.resetAllMocks());

  it("refreshes saved metadata while retaining draft values and their original revision", async () => {
    api.listRunnerProfiles.mockResolvedValueOnce([runner("local", "Original runner")]);
    const user = userEvent.setup();
    render(<RunnerSettings />);
    const name = await screen.findByRole("textbox", {name: "Profile name"});
    await waitFor(() => expect(name).toHaveValue("Original runner"));
    await user.clear(name);
    await user.type(name, "Operator draft");
    api.listRunnerProfiles.mockResolvedValue([{...runner("local", "Core renamed runner"), revision: 2}]);
    act(() => notifyRuntimeSettingsChanged("runner-profiles"));
    expect(await screen.findByText(/Runner profiles changed/)).toBeVisible();
    expect(name).toHaveValue("Operator draft");
    api.updateRunnerProfile.mockRejectedValue(new Error("Revision conflict; refresh runner profiles."));
    await user.click(screen.getByRole("button", {name: "Save and check"}));
    await waitFor(() => expect(api.updateRunnerProfile).toHaveBeenCalledWith("local", expect.objectContaining({name: "Operator draft", expectedRevision: 1})));
    expect(await screen.findByRole("alert")).toHaveTextContent("Revision conflict");
    await user.click(screen.getByRole("button", {name: "Refresh runner profiles"}));
    await waitFor(() => expect(name).toHaveValue("Core renamed runner"));
  });

  it("keeps typed edits when another profile is picked instead of refetching over them", async () => {
    const profiles = [runner("local", "Local Podman"), runner("desktop", "Docker Desktop")];
    api.listRunnerProfiles.mockResolvedValueOnce(profiles).mockResolvedValue(profiles);
    const user = userEvent.setup();
    render(<RunnerSettings />);

    await user.selectOptions(await screen.findByRole("combobox", {name: "Runner profile"}), "desktop");
    const name = screen.getByRole("textbox", {name: "Profile name"});
    expect(name).toHaveValue("Docker Desktop");
    await user.type(name, " (edited)");

    await waitFor(() => expect(name).toHaveValue("Docker Desktop (edited)"));
    expect(api.listRunnerProfiles).toHaveBeenCalledTimes(1);
  });
});

describe("automation runtime VPN profiles", () => {
  beforeEach(() => vi.resetAllMocks());

  it("disables Remove while the delete is pending and removes the profile once", async () => {
    api.getAutomationRuntime.mockResolvedValue(runtime);
    api.listVpnProfiles.mockResolvedValue([vpn("vpn-1", "Office")]);
    let finishDelete!: () => void;
    api.deleteVpnProfile.mockImplementation(() => new Promise<void>((resolve) => { finishDelete = resolve; }));
    const user = userEvent.setup();
    render(<AutomationRuntimeSettings />);

    const remove = await screen.findByRole("button", {name: "Remove Office"});
    await user.click(remove);
    expect(remove).toBeDisabled();

    finishDelete();
    await waitFor(() => expect(screen.queryByRole("button", {name: "Remove Office"})).not.toBeInTheDocument());
    expect(api.deleteVpnProfile).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
