import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { DialogProvider } from "../components/DialogSystem";
import { SettingsPage } from "./SettingsPage";
import { invoke } from "@tauri-apps/api/core";

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
    setupStatus: undefined,
    workspaceState: "ready",
    engagement: undefined,
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
    vi.mocked(invoke).mockImplementation(command => command === "stop_local_backend" ? Promise.reject(new Error("A command still has an uncertain effect")) : Promise.resolve({mode: "local", tokenAvailable: true}));
    renderSettings("/settings#setup-settings");
    await userEvent.click(screen.getByRole("button", {name: "Shut down Core"}));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", {name: "Shut down Core"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("uncertain effect");
    expect(screen.queryByText(/Core has shut down/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Shut down Core"})).toBeEnabled();
  });

  it("does not offer local shutdown for a remote Core", async () => {
    vi.mocked(invoke).mockResolvedValue({mode: "remote", endpoint: "https://remote.test", tokenAvailable: true});
    renderSettings("/settings#setup-settings");
    await screen.findByText("Remote Core selected");
    expect(screen.queryByRole("button", {name: "Shut down Core"})).not.toBeInTheDocument();
  });
});

