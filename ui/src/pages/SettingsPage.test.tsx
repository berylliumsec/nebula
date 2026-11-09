import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { ProviderCatalogEntry, ProviderHealth, SkillCatalogInfo } from "../api/types";
import { DialogProvider } from "../components/DialogSystem";
import { SettingsPage } from "./SettingsPage";

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

const openai: ProviderCatalogEntry = { flavor: "openai", adapter: "openai", displayName: "OpenAI", local: false, defaultBaseUrl: "https://api.openai.com/v1", suggestedKeyEnv: "OPENAI_API_KEY", supportTier: "native" };
const openrouter: ProviderCatalogEntry = { flavor: "openrouter", adapter: "openai_compatible", displayName: "OpenRouter", local: false, defaultBaseUrl: "https://openrouter.ai/api/v1", suggestedKeyEnv: "OPENROUTER_API_KEY", supportTier: "native" };
const savedOpenrouter: ProviderHealth = {
  id: "openrouter-profile", revision: 3, name: "OpenRouter", providerType: "openrouter", kind: "commercial", local: false,
  state: "healthy", enabled: true, endpoint: "https://openrouter.ai/api/v1", models: ["author/model"], modelAllowlist: [],
  defaultModel: "author/model", credentialEnv: "OPENROUTER_API_KEY", permitsSensitiveData: false, autoShareToolResults: false,
  residency: [], options: { openrouter_zdr: true, openrouter_providers: ["relace"] }, metadata: {},
  modelCount: 1, privacy: "cloud", capabilities: [],
};
const savedOpenai: ProviderHealth = {
  ...savedOpenrouter,
  id: "openai-profile",
  name: "OpenAI API",
  providerType: "openai",
  endpoint: "https://api.openai.com",
  credentialEnv: undefined,
  credentialRef: "vault:00000000000000000000000000000001",
  options: { openai_cyber_access_program: "daybreak_blue" },
};

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

describe("shared skill catalog loading", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workspace.providers = [] as never[];
    workspace.providerCatalog = [openai] as never[];
    workspace.engagement = {id: "skills-project"} as never;
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    workspace.api = undefined;
    workspace.engagement = undefined;
  });

  it.each(["empty", "populated", "failed"] as const)("keeps the catalog surface through a delayed %s response", async outcome => {
    let resolveCatalog!: (value: SkillCatalogInfo) => void;
    let rejectCatalog!: (error: Error) => void;
    const catalog = new Promise<SkillCatalogInfo>((resolve, reject) => { resolveCatalog = resolve; rejectCatalog = reject; });
    workspace.api = {
      discoverLocalProviders: vi.fn().mockResolvedValue([]),
      getSkillCatalog: vi.fn().mockReturnValue(catalog),
      listSkills: vi.fn().mockResolvedValue(outcome === "populated" ? [{name: "review", path: "/workspace/.agents/skills/review/SKILL.md", source: "project"}] : []),
    } as unknown as ApiClient;
    renderSettings("/settings#provider-settings");
    const region = screen.getByRole("region", {name: "Shared skill catalogs"});
    expect(region).toHaveAttribute("aria-busy", "true");
    expect(region).toHaveTextContent("Reading skill catalogs…");
    region.focus();
    expect(region).toHaveFocus();
    await act(async () => {
      if (outcome === "failed") rejectCatalog(new Error("Catalog unavailable"));
      else resolveCatalog({projectRoot: "/workspace/.agents/skills", managedRoot: "/var/lib/nebula/.agents/skills"});
    });
    expect(screen.getByRole("region", {name: "Shared skill catalogs"})).toBe(region);
    expect(region).toHaveAttribute("aria-busy", "false");
    expect(region).not.toHaveTextContent("Reading skill catalogs…");
    if (outcome === "failed") expect(within(region).getByRole("alert")).toHaveTextContent("Catalog unavailable");
    else {
      expect(region).toHaveTextContent("/workspace/.agents/skills");
      expect(region).toHaveTextContent("/var/lib/nebula/.agents/skills");
      expect(region).toHaveTextContent(outcome === "populated" ? "review project" : "Add a folder containing SKILL.md");
    }
    await userEvent.click(screen.getByText("Project Policy", {selector: "summary strong"}));
    expect(document.getElementById("project-policy-settings")).toHaveAttribute("open");
  });
});

describe("settings page section routing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workspace.api = undefined;
    workspace.providerCatalog = [openai] as never[];
    workspace.providers = [] as never[];
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

  it.each(["before", "after"])("preserves shell focus acquired %s Settings schedules deep-link focus", (order) => {
    const frames: FrameRequestCallback[] = [];
    const request = vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => {
      frames.push(callback);
      return frames.length;
    });
    const cancel = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
    const shell = document.createElement("header");
    shell.className = "top-bar";
    const shellControl = document.createElement("button");
    shellControl.textContent = "Open top menu";
    shell.append(shellControl);
    document.body.append(shell);
    try {
      if (order === "before") shellControl.focus();
      renderSettings("/settings#provider-settings");
      if (order === "after") shellControl.focus();
      act(() => { for (const callback of [...frames]) callback(0); });
      expect(shellControl).toHaveFocus();
      expect(screen.getByRole("heading", {name: "Model providers"})).not.toHaveFocus();
    } finally {
      shell.remove();
      request.mockRestore();
      cancel.mockRestore();
    }
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

  it("focuses its own deep-link target after the containing lens takes focus", () => {
    const frames: FrameRequestCallback[] = [];
    const request = vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => {
      frames.push(callback);
      return frames.length;
    });
    const cancel = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
    const originalScroll = HTMLElement.prototype.scrollIntoView;
    const scroll = vi.fn();
    HTMLElement.prototype.scrollIntoView = scroll;
    try {
      render(<MemoryRouter initialEntries={["/settings"]}><DialogProvider>
        <SettingsPage />
        <div role="dialog" aria-label="Setting lens" tabIndex={-1}><SettingsPage embeddedTarget="operator-settings" /></div>
      </DialogProvider></MemoryRouter>);

      const lens = screen.getByRole("dialog", { name: "Setting lens" });
      lens.focus();
      act(() => { for (const callback of [...frames]) callback(0); });
      const heading = within(lens).getByRole("heading", { name: "Operator profiles" });
      expect(heading).toHaveFocus();
      expect(scroll.mock.contexts).toContain(heading.closest("section"));
      expect(scroll).toHaveBeenCalledWith({ block: "start", behavior: "instant" });
    } finally {
      if (originalScroll) HTMLElement.prototype.scrollIntoView = originalScroll;
      else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
      request.mockRestore();
      cancel.mockRestore();
    }
  });
});

describe("settings page dialogs", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workspace.api = undefined;
    workspace.providerCatalog = [openai] as never[];
    workspace.providers = [] as never[];
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

  it("saves OpenRouter ZDR and allowed upstreams as profile options", async () => {
    workspace.providerCatalog = [openrouter] as never[];
    workspace.addProvider.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderSettings("/settings#provider-settings");

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = screen.getByRole("dialog", { name: "Add model provider" });
    expect(within(dialog).getByRole("checkbox", { name: /Require zero data retention/ })).not.toBeChecked();
    await user.click(within(dialog).getByRole("checkbox", { name: /Require zero data retention/ }));
    await user.click(within(dialog).getByRole("button", { name: "JSON" }));
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Upstream provider allowlist JSON" }), { target: { value: '["relace"]' } });
    await user.click(within(dialog).getByRole("button", { name: "Add provider" }));

    await waitFor(() => expect(workspace.addProvider).toHaveBeenCalledWith(expect.objectContaining({
      providerType: "openrouter",
      options: expect.objectContaining({ openrouter_zdr: true, openrouter_providers: ["relace"] }),
    })));
  });

  it("saves an OpenAI API key and Daybreak Blue in its provider profile", async () => {
    const createCredential = vi.fn().mockResolvedValue({ reference: "vault:00000000000000000000000000000001" });
    workspace.api = {
      createCredential,
      deleteCredential: vi.fn(),
      discoverLocalProviders: vi.fn().mockResolvedValue([]),
    } as unknown as ApiClient;
    workspace.addProvider.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderSettings("/settings#provider-settings");

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = screen.getByRole("dialog", { name: "Add model provider" });
    expect(within(dialog).getByRole("combobox", { name: "Provider type" })).toHaveValue("openai");
    await user.type(within(dialog).getByLabelText("Credential", { exact: true }), "synthetic-openai-key");
    await user.click(within(dialog).getByRole("checkbox", { name: /Request Daybreak Blue/ }));
    await user.click(within(dialog).getByRole("button", { name: "Add provider" }));

    await waitFor(() => expect(createCredential).toHaveBeenCalledWith("synthetic-openai-key", "vault"));
    expect(workspace.addProvider).toHaveBeenCalledWith(expect.objectContaining({
      providerType: "openai",
      credentialRef: "vault:00000000000000000000000000000001",
      credentialEnv: undefined,
      options: { openai_cyber_access_program: "daybreak_blue" },
    }));
    expect(screen.queryByText("synthetic-openai-key")).not.toBeInTheDocument();
  });

  it("restores and can clear Daybreak Blue on an OpenAI provider", async () => {
    workspace.providerCatalog = [openai] as never[];
    workspace.providers = [savedOpenai] as never[];
    workspace.updateProvider.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderSettings("/settings#provider-settings");

    await user.click(screen.getByRole("button", { name: "Edit OpenAI API" }));
    const dialog = screen.getByRole("dialog", { name: "Edit OpenAI API" });
    const daybreak = within(dialog).getByRole("checkbox", { name: /Request Daybreak Blue/ });
    expect(daybreak).toBeChecked();
    await user.click(daybreak);
    await user.click(within(dialog).getByRole("button", { name: "Save provider" }));

    await waitFor(() => expect(workspace.updateProvider).toHaveBeenCalledWith(
      savedOpenai.id,
      expect.objectContaining({ options: {} }),
    ));
  });

  it("stores the OpenRouter account management key as a separate credential reference", async () => {
    const createCredential = vi.fn().mockResolvedValue({ reference: "vault:router-management" });
    workspace.api = {
      createCredential,
      deleteCredential: vi.fn(),
      discoverLocalProviders: vi.fn().mockResolvedValue([]),
      listOpenRouterUpstreamProviders: vi.fn().mockResolvedValue([]),
    } as unknown as ApiClient;
    workspace.providerCatalog = [openrouter] as never[];
    workspace.addProvider.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderSettings("/settings#provider-settings");

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = screen.getByRole("dialog", { name: "Add model provider" });
    await user.type(within(dialog).getByLabelText(/^Account management key/), "management-secret");
    await user.click(within(dialog).getByRole("button", { name: "Add provider" }));

    await waitFor(() => expect(createCredential).toHaveBeenCalledWith("management-secret", "vault"));
    expect(createCredential).toHaveBeenCalledTimes(1);
    expect(workspace.addProvider).toHaveBeenCalledWith(expect.objectContaining({
      providerType: "openrouter",
      credentialEnv: "OPENROUTER_API_KEY",
      openrouterManagementSecretRef: "vault:router-management",
    }));
  });

  it("loads the saved routing policy and can clear both options", async () => {
    workspace.providerCatalog = [openrouter] as never[];
    workspace.providers = [savedOpenrouter] as never[];
    workspace.updateProvider.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderSettings("/settings#provider-settings");

    await user.click(screen.getByRole("button", { name: "Edit OpenRouter" }));
    const dialog = screen.getByRole("dialog", { name: "Edit OpenRouter" });
    const zdr = within(dialog).getByRole("checkbox", { name: /Require zero data retention/ });
    expect(zdr).toBeChecked();
    expect(within(dialog).getByLabelText("Allowed upstream providers")).toHaveTextContent("relace");
    await user.click(zdr);
    await user.click(within(dialog).getByRole("button", { name: "Allow any provider" }));
    await user.click(within(dialog).getByRole("button", { name: "Save provider" }));

    await waitFor(() => expect(workspace.updateProvider).toHaveBeenCalledWith(
      savedOpenrouter.id,
      expect.objectContaining({ options: {} }),
    ));
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

  it("explains the working ceiling where the context window is configured", async () => {
    const user = userEvent.setup();
    renderSettings("/settings");

    await user.click(screen.getByRole("button", { name: "Add provider" }));
    const dialog = screen.getByRole("dialog", { name: "Add model provider" });
    // A window above the 200,000-token working ceiling is the opt-in to a
    // larger working context (resolve_context_limits).
    expect(within(dialog).getByLabelText("Context window (tokens)")).toHaveAccessibleDescription(
      "Leave blank to use the model's published window. Conversations are compacted by about 200,000 tokens even on larger windows; a value above 200,000 opts into a larger working context. A value here also caps the window.",
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
