import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {MemoryRouter} from "react-router-dom";
import {beforeEach, describe, expect, it, vi} from "vitest";
import type {HarnessProfile, McpServerProfile} from "../api/types";
import {DialogProvider} from "./DialogSystem";
import {HarnessSettings} from "./HarnessSettings";

const profile: HarnessProfile = {
  id: "saved", name: "Local fixture", kind: "grok_acp", connectionMode: "spawn", transport: "stdio",
  executable: "/disposable/inert-fixture", authMode: "existing_session", models: ["fixture-model"],
  enabled: true, localOnly: true, permitsSensitiveData: false, autoShareToolResults: false, revision: 1,
  nativeCapabilities: {workspaceAccess: "none", shell: false, webSearch: false, webFetch: false, browser: false, computerUse: false, imageGeneration: false, skills: false, subagents: false},
};
const api = {listHarnesses: vi.fn(), listMcpServers: vi.fn(), createHarness: vi.fn(), checkHarness: vi.fn(), testHarnessTurn: vi.fn(), importMcpServers: vi.fn(), mcpServerSchema: vi.fn(), updateMcpServer: vi.fn()};
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api, coreState: "online", previewMode: false})}));
vi.mock("../diagnostics", () => ({logCaughtDiagnostic: vi.fn(), DiagnosticErrorNotice: ({error}: {error: string}) => <div role="alert">{error}</div>}));

describe("harness settings persistence boundaries", () => {
  beforeEach(() => {vi.resetAllMocks(); api.listMcpServers.mockResolvedValue([]);});
  for (const failure of ["health", "catalog"] as const) {
    it(`keeps a saved profile discoverable after ${failure} failure and retries without creating it again`, async () => {
      let saved = false;
      let fail = true;
      api.createHarness.mockImplementation(async () => {saved = true; return profile;});
      api.listHarnesses.mockImplementation(async () => {
        if (saved && failure === "catalog" && fail) throw new Error("Catalog unavailable");
        return saved ? [profile] : [];
      });
      api.checkHarness.mockImplementation(async () => {
        if (failure === "health" && fail) throw new Error("Health unavailable");
        return {healthy: true};
      });
      const user = userEvent.setup();
      render(<MemoryRouter><DialogProvider><HarnessSettings /></DialogProvider></MemoryRouter>);
      await user.click(screen.getByRole("button", {name: "Add Grok"}));
      const dialog = screen.getByRole("dialog");
      await user.type(within(dialog).getByRole("textbox", {name: "Name"}), profile.name);
      await user.type(within(dialog).getByRole("textbox", {name: "Absolute Grok executable path"}), profile.executable!);
      await user.click(within(dialog).getByRole("button", {name: "Save harness"}));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      expect(screen.getByRole("heading", {name: profile.name})).toBeVisible();
      expect(screen.getByRole("alert")).toHaveTextContent(/saved/i);
      fail = false;
      await user.click(screen.getByRole("button", {name: "Check"}));
      await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
      expect(api.createHarness).toHaveBeenCalledTimes(1);
      expect(api.checkHarness).toHaveBeenCalledTimes(2);
    });
  }
});

it("keeps Check free and makes provider usage explicit for Test turn", async () => {
  api.listHarnesses.mockResolvedValue([{...profile, authenticationState: "verified", sessionState: "unverified", turnState: "unverified"}]);
  api.testHarnessTurn.mockResolvedValue({healthy: true});
  render(<MemoryRouter><DialogProvider><HarnessSettings /></DialogProvider></MemoryRouter>);
  expect(await screen.findByText(/Sign-in: verified · Session: unverified · Model turn: unverified/)).toBeVisible();
  const test = screen.getByRole("button", {name: "Test turn"});
  expect(test).toHaveAttribute("title", expect.stringContaining("provider quota"));
  expect(api.testHarnessTurn).not.toHaveBeenCalled();
  await userEvent.click(test);
  await waitFor(() => expect(api.testHarnessTurn).toHaveBeenCalledWith(profile.id));
});

it("explains exhausted balance without offering an immediate turn retry", async () => {
  vi.clearAllMocks();
  api.listHarnesses.mockResolvedValue([{
    ...profile, authenticationState: "verified", turnState: "failed",
    lastTurnFailureReason: "quota_exhausted",
  }]);
  render(<MemoryRouter><DialogProvider><HarnessSettings /></DialogProvider></MemoryRouter>);
  expect(await screen.findByRole("alert")).toHaveTextContent(/Restore balance or wait for reset/);
  expect(screen.queryByRole("button", {name: "Retry turn"})).not.toBeInTheDocument();
  expect(api.testHarnessTurn).not.toHaveBeenCalled();
});

it("shows harnesses independently of a failed MCP catalog and retries in place", async () => {
  api.listHarnesses.mockResolvedValue([profile]);
  api.listMcpServers.mockRejectedValueOnce(new Error("MCP unavailable")).mockResolvedValue([]);
  render(<MemoryRouter><DialogProvider><HarnessSettings /></DialogProvider></MemoryRouter>);
  expect(await screen.findByRole("heading", {name: profile.name})).toBeVisible();
  await userEvent.click(await screen.findByRole("button", {name: "Retry catalogs"}));
  await waitFor(() => expect(screen.queryByRole("button", {name: "Retry catalogs"})).not.toBeInTheDocument());
  expect(screen.getByRole("heading", {name: profile.name})).toBeVisible();
});

it("shows harnesses while the MCP catalog is still pending", async () => {
  api.listHarnesses.mockResolvedValue([profile]);
  api.listMcpServers.mockReturnValue(new Promise(() => {}));
  render(<MemoryRouter><DialogProvider><HarnessSettings /></DialogProvider></MemoryRouter>);
  expect(await screen.findByRole("heading", {name: profile.name})).toBeVisible();
});

for (const vendor of ["Grok", "Codex"] as const) {
  it(`saves a separate ${vendor} account home and shows its host login command`, async () => {
    api.listHarnesses.mockResolvedValue([]);
    api.listMcpServers.mockResolvedValue([]);
    api.createHarness.mockResolvedValue({...profile, homeDirectory: "/accounts/work account"});
    api.checkHarness.mockResolvedValue({healthy: true});
    render(<MemoryRouter><DialogProvider><HarnessSettings /></DialogProvider></MemoryRouter>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", {name: `Add ${vendor}`}));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", {name: "Name"}), `${vendor} Work`);
    await user.type(within(dialog).getByRole("textbox", {name: vendor === "Grok" ? "Absolute Grok executable path" : "Absolute executable path"}), "/bin/inert");
    await user.click(within(dialog).getByText("Account folder", {exact: true}));
    await user.type(within(dialog).getByRole("textbox", {name: "Account home folder"}), "/accounts/work account");
    expect(within(dialog).getByRole("button", {name: "Browse folders"})).toBeVisible();
    expect(dialog.querySelector("pre")?.textContent).toContain(`${vendor === "Grok" ? "GROK_HOME" : "CODEX_HOME"}='/accounts/work account'`);
    if (vendor === "Codex") expect(dialog.querySelector("pre")?.textContent).toContain('cli_auth_credentials_store="file"');
    await user.click(within(dialog).getByRole("button", {name: "Save harness"}));
    await waitFor(() => expect(api.createHarness).toHaveBeenCalledWith(expect.objectContaining({home_directory: "/accounts/work account", name: `${vendor} Work`})));
  });
}

const importedServer: McpServerProfile = {
  id: "mcp-burp", name: "burp", transport: "stdio", command: "/usr/bin/npx", arguments: ["-y", "burp-mcp"], authMode: "none",
  enabled: false, required: false, trustedStdio: false, defaultApproval: "risk_based", toolOverrides: {}, tools: [], revision: 1,
};

describe("MCP import entry points", () => {
  beforeEach(() => {vi.resetAllMocks(); api.listHarnesses.mockResolvedValue([]);});

  it("offers import from the empty state and the section heading", async () => {
    api.listMcpServers.mockResolvedValue([]);
    render(<MemoryRouter><DialogProvider><HarnessSettings /></DialogProvider></MemoryRouter>);
    await userEvent.click(await screen.findByRole("button", {name: "Import from file"}));
    expect(screen.getByRole("dialog", {name: "Import MCP servers"})).toBeVisible();
    await userEvent.click(screen.getByRole("button", {name: "Close import dialog"}));
    await userEvent.click(screen.getByRole("button", {name: "Import"}));
    expect(screen.getByRole("dialog", {name: "Import MCP servers"})).toBeVisible();
  });

  it("reloads the list from Core after import and names the next step", async () => {
    let imported = false;
    api.listMcpServers.mockImplementation(async () => imported ? [importedServer] : []);
    api.importMcpServers.mockImplementation(async ({dryRun}: {dryRun: boolean}) => {
      if (!dryRun) imported = true;
      return {dryRun, created: 1, replaced: 0, skipped: 0, invalid: 1, entries: [
        {sourceName: "burp", name: "burp", action: "create", transport: "stdio", command: "/usr/bin/npx", arguments: ["-y", "burp-mcp"], secrets: [], warnings: []},
        {sourceName: "recon", name: "recon", action: "invalid", arguments: [], secrets: [], warnings: [], error: "not installed"},
      ]};
    });
    const user = userEvent.setup();
    render(<MemoryRouter><DialogProvider><HarnessSettings /></DialogProvider></MemoryRouter>);
    await user.click(await screen.findByRole("button", {name: "Import from file"}));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("textbox", {name: "Configuration"}));
    await user.paste('{"mcpServers": {"burp": {"command": "npx"}, "recon": {"command": "recon-mcp"}}}');
    await user.click(within(dialog).getByRole("button", {name: "Preview"}));
    await user.click(await within(dialog).findByRole("button", {name: "Import 1 server"}));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByRole("heading", {name: "burp"})).toBeVisible();
    const notice = screen.getByText(/Imported 1 server from Pasted configuration/).closest(".surface-notice")!;
    expect(notice).toHaveTextContent("1 could not be imported: recon (not installed)");
    expect(screen.getByText("Local program · not trusted")).toBeVisible();
    expect(screen.getByText("Trust this local program in Edit before probing.")).toBeVisible();
    expect(screen.getByRole("button", {name: "Probe"})).toBeDisabled();
    expect(screen.getByRole("button", {name: "Enable"})).toBeDisabled();
    await user.click(within(notice as HTMLElement).getByRole("button", {name: "Dismiss import notice"}));
    expect(screen.queryByText(/Imported 1 server/)).not.toBeInTheDocument();
  });

  it("asks trusted but unprobed servers to probe before enabling", async () => {
    api.listMcpServers.mockResolvedValue([{...importedServer, trustedStdio: true}]);
    render(<MemoryRouter><DialogProvider><HarnessSettings /></DialogProvider></MemoryRouter>);
    expect(await screen.findByText("Probe to list its tools, then enable.")).toBeVisible();
    expect(screen.getByRole("button", {name: "Probe"})).toBeEnabled();
  });
});

it("edits an MCP server without resetting its saved working directory", async () => {
  vi.resetAllMocks();
  api.listHarnesses.mockResolvedValue([]);
  api.listMcpServers.mockResolvedValue([importedServer]);
  api.updateMcpServer.mockResolvedValue({...importedServer, trustedStdio: true});
  const user = userEvent.setup();
  render(<MemoryRouter><DialogProvider><HarnessSettings /></DialogProvider></MemoryRouter>);
  await user.click(await screen.findByRole("button", {name: "Edit burp"}));
  const dialog = screen.getByRole("dialog", {name: "Edit MCP server"});
  await user.click(within(dialog).getByRole("checkbox", {name: /I trust this local program/}));
  await user.click(within(dialog).getByRole("button", {name: "Save MCP server"}));
  await waitFor(() => expect(api.updateMcpServer).toHaveBeenCalledTimes(1));
  const [, changes] = api.updateMcpServer.mock.calls[0];
  expect(changes).toMatchObject({trusted_stdio: true});
  expect(changes).not.toHaveProperty("cwd_policy");
});
