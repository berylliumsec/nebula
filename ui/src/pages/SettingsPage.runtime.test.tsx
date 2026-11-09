import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { SetupImagePreparation, SetupStatus } from "../api/types";
import { DialogProvider } from "../components/DialogSystem";
import { SettingsPage } from "./SettingsPage";
import { invoke } from "@tauri-apps/api/core";
import { logCaughtDiagnostic } from "../diagnostics";

const { workspace } = vi.hoisted(() => ({
  workspace: {
    api: undefined as ApiClient | undefined,
    previewMode: false,
    health: undefined,
    providers: [] as never[],
    providerCatalog: [] as never[],
    refreshProvider: vi.fn(),
    reverifyProvider: vi.fn(),
    addProvider: vi.fn(),
    updateProvider: vi.fn(),
    setProviderEnabled: vi.fn(),
    deleteProvider: vi.fn(),
    operatorProfiles: [] as never[],
    createOperatorProfile: vi.fn(),
    updateOperatorProfile: vi.fn(),
    activateOperatorProfile: vi.fn(),
    deleteOperatorProfile: vi.fn(),
    refreshSetupRuntime: vi.fn(),
    setupStatus: undefined as SetupStatus | undefined,
    workspaceState: "ready",
    engagement: undefined as {id: string} | undefined,
  },
}));

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));
vi.mock("../state/ThemeContext", async (importOriginal) => ({
  ...await importOriginal<typeof import("../state/ThemeContext")>(),
  useTheme: () => ({ preference: "dark", resolvedTheme: "dark", setPreference: vi.fn(), cycleTheme: vi.fn() }),
}));
vi.mock("../state/uiZoom", () => ({ useUiZoom: () => ({ zoom: 1, supported: false, change: vi.fn() }), UI_ZOOM_DEFAULT: 1, UI_ZOOM_STEPS: [1] }));
vi.mock("../hooks/useCredentialVault", () => ({ useCredentialVault: () => ({ state: "unlocked", available: true, refresh: vi.fn() }), vaultUnavailableNote: () => "" }));
vi.mock("../state/ChromeContext", () => ({ useChrome: () => ({ toolbarHost: null, trailingToolbarHost: null }) }));
vi.mock("../diagnostics", () => ({
  logCaughtDiagnostic: vi.fn(),
  DiagnosticErrorNotice: ({ error }: { error: string }) => <div role="alert">{error}</div>,
  DiagnosticsPanel: () => null,
}));
vi.mock("../guides/ShowMeHow", () => ({ ShowMeHow: () => null }));
vi.mock("../components/ToolingSettings", () => ({ AutomationRuntimeSettings: () => null, RunnerSettings: () => null }));
vi.mock("../components/EnvironmentSettings", () => ({ EnvironmentSettings: () => null }));
vi.mock("../components/StorageIntegrations", () => ({ StorageIntegrations: () => null }));
vi.mock("../components/HarnessSettings", () => ({ HarnessSettings: () => null }));
vi.mock("../components/PostToolAssistantSettings", () => ({ PostToolAssistantSettings: () => null }));
vi.mock("../components/ButlerInstructionsSettings", () => ({ ButlerInstructionsSettings: () => null }));
vi.mock("../components/ButlerCheckIntervalSettings", () => ({ ButlerCheckIntervalSettings: () => null }));
vi.mock("../components/TypeSafeIntegrationSettings", () => ({ TypeSafeIntegrationSettings: () => null }));
vi.mock("../components/EngagementPolicySettings", () => ({ EngagementPolicySettings: () => null }));
vi.mock("../components/ReleaseSettingsPanel", () => ({ ReleaseSettingsPanel: () => null }));
vi.mock("../components/HostCpuBudgetSettings", () => ({ HostCpuBudgetSettings: () => null }));
vi.mock("../components/HostPressureSettings", () => ({ HostPressureSettings: () => null }));
vi.mock("../components/MemoryBudgetSettings", () => ({ MemoryBudgetSettings: () => null }));
vi.mock("../components/RuntimeUsagePanel", () => ({ RuntimeUsagePanel: () => null }));
vi.mock("../components/WebSearchRuntimeSettings", () => ({ WebSearchRuntimeSettings: () => null }));
vi.mock("../components/DevicePairingSettings", () => ({ DevicePairingSettings: () => null }));
vi.mock("../components/ProviderHealthCard", () => ({ ProviderHealthCard: ({ provider, onEdit }: { provider: { name: string }; onEdit: (provider: unknown) => void }) => <button type="button" onClick={() => onEdit(provider)}>Edit {provider.name}</button> }));
vi.mock("../components/CompactSettingsList", () => ({ CompactSettingsList: () => <div>Compact settings list</div> }));
vi.mock("../components/SettingsSaveFeedback", () => ({ announceSettingsSaved: vi.fn(), SettingsSaveFeedback: () => null }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

function renderSettings(route: string) {
  return render(<MemoryRouter initialEntries={[route]}><DialogProvider><SettingsPage /></DialogProvider></MemoryRouter>);
}

describe("explicit local Core shutdown", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("__TAURI_INTERNALS__", {});
    workspace.api = undefined;
    workspace.engagement = undefined;
    workspace.setupStatus = undefined;
    workspace.providers = [] as never[];
    workspace.providerCatalog = [] as never[];
    vi.mocked(invoke).mockResolvedValue({mode: "local", tokenAvailable: true});
  });
  afterEach(() => vi.unstubAllGlobals());

  it("waits for observed shutdown before reporting completion", async () => {
    let complete!: () => void;
    const pending = new Promise<void>(resolve => { complete = resolve; });
    vi.mocked(invoke).mockImplementation(command => command === "stop_local_backend" ? pending : Promise.resolve({mode: "local", tokenAvailable: true}));
    renderSettings("/settings#setup-settings");
    await userEvent.click(screen.getByRole("button", {name: "Shut down Core"}));
    expect(vi.mocked(invoke).mock.calls.some(([command]) => command === "stop_local_backend")).toBe(false);
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", {name: "Shut down Core"}));
    expect(screen.queryByText(/Core has shut down/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Waiting for Core…"})).toBeDisabled();
    await act(async () => complete());
    expect(await screen.findByText(/Core has shut down/)).toBeVisible();
  });

  it("leaves a blocked shutdown actionable without claiming completion", async () => {
    const failure = new Error("A command still has an uncertain effect");
    vi.mocked(invoke).mockImplementation(command => command === "stop_local_backend" ? Promise.reject(failure) : Promise.resolve({mode: "local", tokenAvailable: true}));
    renderSettings("/settings#setup-settings");
    await userEvent.click(screen.getByRole("button", {name: "Shut down Core"}));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", {name: "Shut down Core"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("uncertain effect");
    expect(screen.queryByText(/Core has shut down/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Shut down Core"})).toBeEnabled();
    expect(logCaughtDiagnostic).toHaveBeenCalledWith("interface.settings_page.core_shutdown_failed", "The local Core could not shut down.", failure, "core-shutdown");
  });

  it("does not offer local shutdown for a remote Core", async () => {
    vi.mocked(invoke).mockResolvedValue({mode: "remote", endpoint: "https://remote.test", tokenAvailable: true});
    renderSettings("/settings#setup-settings");
    await screen.findByText("Remote Core selected");
    expect(screen.queryByRole("button", {name: "Shut down Core"})).not.toBeInTheDocument();
  });
});

function imageSetup(projectId: string, phase: SetupImagePreparation["phase"] = "preparing_image"): SetupStatus {
  const active = ["queued", "resolving_runtime", "preparing_image", "cancelling"].includes(phase);
  const failed = phase === "error" || phase === "cancelled";
  return {
    applicationStage: "ready", stageDetail: "Ready", retryable: false, recoveryActions: [], core: {status: "ready"},
    scratchProjectId: "host-scratch",
    terminal: {status: active ? "preparing_image" : failed ? "error" : "ready", candidates: [], imagePreparation: {
      projectId, operationId: `${projectId}-operation`, phase, progressIndeterminate: active,
      canCancel: phase === "preparing_image", canRetry: failed, detail: `${projectId} image ${phase}`,
    }}, assistant: {status: "needs_model"},
  };
}

function projectApi() {
  return {
    setupStatus: vi.fn(), cancelSetupImage: vi.fn().mockResolvedValue(undefined), retrySetupImage: vi.fn().mockResolvedValue(undefined),
    discoverLocalProviders: vi.fn().mockResolvedValue([]), getSkillCatalog: vi.fn().mockResolvedValue({projectRoot: "", managedRoot: ""}),
    listSkills: vi.fn().mockResolvedValue([]),
  };
}

describe("selected Project image preparation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workspace.engagement = {id: "project-a"};
    workspace.setupStatus = imageSetup("host-scratch");
    workspace.providers = [] as never[];
    workspace.providerCatalog = [] as never[];
  });
  afterEach(() => { workspace.api = undefined; workspace.engagement = undefined; workspace.setupStatus = undefined; });

  it("waits for the selected snapshot, preserves failed cancellation and retries its own image", async () => {
    const api = projectApi();
    let observed = imageSetup("project-a");
    let finish!: (value: SetupStatus) => void;
    api.setupStatus.mockImplementationOnce(() => new Promise(resolve => {finish = resolve;})).mockImplementation(async () => observed);
    api.cancelSetupImage.mockRejectedValueOnce(new Error("Cancellation temporarily blocked")).mockImplementation(async () => { observed = imageSetup("project-a", "cancelled"); });
    api.retrySetupImage.mockImplementation(async () => { observed = imageSetup("project-a", "ready"); });
    workspace.api = api as unknown as ApiClient;
    renderSettings("/settings#setup-settings");
    expect(screen.getByText("Checking Project setup…")).toBeVisible();
    expect(screen.queryByRole("button", {name: "Cancel"})).not.toBeInTheDocument();
    await act(async () => finish(observed));
    await userEvent.click(screen.getByRole("button", {name: "Cancel"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("Cancellation temporarily blocked");
    expect(screen.getByRole("button", {name: "Cancel"})).toBeEnabled();
    await userEvent.click(screen.getByRole("button", {name: "Cancel"}));
    await userEvent.click(await screen.findByRole("button", {name: "Retry preparation"}));
    expect(await screen.findByText("Kali runtime ready")).toBeVisible();
    expect(api.cancelSetupImage.mock.calls).toEqual([["project-a-operation", "project-a"], ["project-a-operation", "project-a"]]);
    expect(api.retrySetupImage).toHaveBeenCalledWith("project-a");
    expect(workspace.refreshSetupRuntime).not.toHaveBeenCalled();
    expect(api.setupStatus.mock.calls.every(([, projectId]) => projectId === "project-a")).toBe(true);
  });

  it("ignores a late previous Project snapshot and cancels only the newly selected operation", async () => {
    const api = projectApi();
    let finishOld!: (value: SetupStatus) => void;
    api.setupStatus.mockImplementation((_signal, projectId) => projectId === "project-a"
      ? new Promise(resolve => {finishOld = resolve;}) : Promise.resolve(imageSetup("project-b")));
    workspace.api = api as unknown as ApiClient;
    const view = renderSettings("/settings#setup-settings");
    workspace.engagement = {id: "project-b"};
    view.rerender(<MemoryRouter initialEntries={["/settings#setup-settings"]}><DialogProvider><SettingsPage /></DialogProvider></MemoryRouter>);
    expect(await screen.findByText("project-b image preparing_image")).toBeVisible();
    expect(api.setupStatus.mock.calls[0][0].aborted).toBe(true);
    await act(async () => finishOld(imageSetup("project-a")));
    expect(screen.queryByText("project-a image preparing_image")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "Cancel"}));
    expect(api.cancelSetupImage).toHaveBeenCalledWith("project-b-operation", "project-b");
    expect(api.cancelSetupImage).toHaveBeenCalledOnce();
  });

  it("never offers image controls for a different Project or falls back to the host scratch Project", async () => {
    const api = projectApi();
    api.setupStatus.mockResolvedValue(imageSetup("another-project", "error"));
    workspace.api = api as unknown as ApiClient;
    renderSettings("/settings#setup-settings");
    await screen.findByText("Needs attention");
    expect(screen.queryByRole("button", {name: "Cancel"})).not.toBeInTheDocument();
    expect(screen.queryByRole("button", {name: "Retry preparation"})).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", {name: "Check again"}));
    expect(workspace.refreshSetupRuntime).toHaveBeenCalledOnce();
    expect(api.retrySetupImage).not.toHaveBeenCalled();
    expect(api.cancelSetupImage).not.toHaveBeenCalled();
    expect(api.setupStatus).toHaveBeenCalledWith(expect.any(AbortSignal), "project-a");
  });
});

