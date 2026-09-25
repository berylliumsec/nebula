import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ProviderCatalogEntry } from "../api/types";
import { DialogProvider } from "../components/DialogSystem";
import { SettingsPage } from "./SettingsPage";

const { workspace } = vi.hoisted(() => ({
  workspace: {
    api: undefined,
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
vi.mock("../state/ThemeContext", () => ({ useTheme: () => ({ preference: "dark", resolvedTheme: "dark", setPreference: vi.fn(), cycleTheme: vi.fn() }) }));
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
vi.mock("../components/HarnessSettings", () => ({ HarnessSettings: () => null }));
vi.mock("../components/PostToolAssistantSettings", () => ({ PostToolAssistantSettings: () => null }));
vi.mock("../components/TypeSafeIntegrationSettings", () => ({ TypeSafeIntegrationSettings: () => null }));
vi.mock("../components/EngagementPolicySettings", () => ({ EngagementPolicySettings: () => null }));
vi.mock("../components/ReleaseSettingsPanel", () => ({ ReleaseSettingsPanel: () => null }));
vi.mock("../components/DevicePairingSettings", () => ({ DevicePairingSettings: () => null }));
vi.mock("../components/ProviderHealthCard", () => ({ ProviderHealthCard: () => null }));
vi.mock("../components/CompactSettingsList", () => ({ CompactSettingsList: () => <div>Compact settings list</div> }));
vi.mock("../components/SettingsSaveFeedback", () => ({ announceSettingsSaved: vi.fn(), SettingsSaveFeedback: () => null }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const openai: ProviderCatalogEntry = { flavor: "openai", adapter: "openai", displayName: "OpenAI", local: false, defaultBaseUrl: "https://api.openai.com/v1", suggestedKeyEnv: "OPENAI_API_KEY", supportTier: "native" };

function NavigateButton({ to }: { to: string }) {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate(to)}>Navigate to {to}</button>;
}

function RouterHash() {
  return <output aria-label="Router hash">{useLocation().hash}</output>;
}

function renderSettings(route = "/settings") {
  return render(<MemoryRouter initialEntries={[route]}><DialogProvider>
    <SettingsPage />
    <NavigateButton to="/settings#provider-settings" />
    <RouterHash />
  </DialogProvider></MemoryRouter>);
}

describe("settings page section routing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workspace.providerCatalog = [openai] as never[];
    window.history.replaceState(null, "", "/");
  });

  it("switches to the section named by an in-app navigation", async () => {
    const user = userEvent.setup();
    renderSettings("/settings");
    expect(screen.getByRole("link", { name: "Setup" })).toHaveAttribute("aria-current", "page");

    await user.click(screen.getByRole("button", { name: "Navigate to /settings#provider-settings" }));

    await waitFor(() => expect(screen.getByRole("link", { name: "Advanced settings" })).toHaveAttribute("aria-current", "page"));
    expect(document.getElementById("models-settings")).toHaveAttribute("open");
    await waitFor(() => expect(screen.getByRole("heading", { name: "Model providers" })).toHaveFocus());
  });

  it("keeps the router location in step with the selected tab and group", async () => {
    const user = userEvent.setup();
    renderSettings("/settings");

    await user.click(screen.getByRole("link", { name: "Diagnostics settings and recent errors" }));
    expect(screen.getByRole("link", { name: "Diagnostics settings and recent errors" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByLabelText("Router hash")).toHaveTextContent("#diagnostics-settings");

    await user.click(screen.getByRole("link", { name: "Advanced settings" }));
    expect(screen.getByLabelText("Router hash")).toHaveTextContent("#models-settings");

    await user.click(screen.getByText("Automation", { selector: "summary strong" }));
    expect(document.getElementById("automation-settings")).toHaveAttribute("open");
    expect(screen.getByLabelText("Router hash")).toHaveTextContent("#automation-settings");
  });

  it("focuses its own deep-link target when another settings page is mounted behind it", async () => {
    render(<MemoryRouter initialEntries={["/settings"]}><DialogProvider>
      <SettingsPage />
      <div role="dialog" aria-label="Setting lens"><SettingsPage embeddedTarget="operator-settings" /></div>
    </DialogProvider></MemoryRouter>);

    const lens = screen.getByRole("dialog", { name: "Setting lens" });
    await waitFor(() => expect(within(lens).getByRole("heading", { name: "Operator profiles" })).toHaveFocus());
  });
});

describe("settings page dialogs", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workspace.providerCatalog = [openai] as never[];
    window.history.replaceState(null, "", "/");
  });

  it("keeps the provider dialog open until a save settles", async () => {
    let reject!: (error: Error) => void;
    workspace.addProvider.mockImplementation(() => new Promise((_resolve, rejectSave) => { reject = rejectSave; }));
    const user = userEvent.setup();
    renderSettings("/settings");

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = screen.getByRole("dialog", { name: "Add model provider" });
    await user.click(within(dialog).getByRole("button", { name: "Add provider" }));

    expect(within(dialog).getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Close provider dialog" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Add model provider" })).toBe(dialog);

    reject(new Error("The provider rejected the credential."));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The provider rejected the credential.");
    expect(within(dialog).getByRole("button", { name: "Close provider dialog" })).toBeEnabled();
  });

  it("describes the output default Core sizes from the model", async () => {
    const user = userEvent.setup();
    renderSettings("/settings");

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = screen.getByRole("dialog", { name: "Add model provider" });
    const field = within(dialog).getByLabelText("Maximum output tokens");

    // A blank field no longer means a flat 2,048: Core sizes each reply from
    // the model's own limit, held to a quarter of the window
    // (resolve_context_limits).
    expect(field).toHaveAttribute("placeholder", "Sized from the model");
    expect(field).toHaveAccessibleDescription(
      "Leave blank to size each reply from the model: its published output limit, up to a quarter of the context window (at least 8,192 tokens), or 2,048 when the window is unknown. A value here caps every reply.",
    );
  });

  it("saves an unattended provider with an opaque systemd credential reference", async () => {
    workspace.addProvider.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderSettings("/settings");

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = screen.getByRole("dialog", { name: "Add model provider" });
    await user.click(within(dialog).getByText("Advanced provider options"));
    await user.type(within(dialog).getByLabelText(/systemd service credential/i), "openai-api-key");
    await user.click(within(dialog).getByRole("button", { name: "Add provider" }));

    await waitFor(() => expect(workspace.addProvider).toHaveBeenCalledWith(expect.objectContaining({
      credentialEnv: undefined,
      credentialRef: "systemd:openai-api-key",
    })));
  });

  it("trims the operator display name and refuses a blank one", async () => {
    workspace.createOperatorProfile.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderSettings("/settings");

    await user.click(screen.getByRole("button", { name: "Add operator" }));
    const dialog = screen.getByRole("dialog", { name: "Add operator" });
    const name = within(dialog).getByRole("textbox", { name: "Display name" });
    expect(within(dialog).getByRole("button", { name: "Save operator" })).toBeDisabled();
    await user.type(name, "   ");
    expect(within(dialog).getByRole("button", { name: "Save operator" })).toBeDisabled();

    await user.clear(name);
    await user.type(name, "  Ada Lovelace  ");
    await user.click(within(dialog).getByRole("button", { name: "Save operator" }));

    await waitFor(() => expect(workspace.createOperatorProfile).toHaveBeenCalledWith({ displayName: "Ada Lovelace", email: undefined, role: undefined }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
