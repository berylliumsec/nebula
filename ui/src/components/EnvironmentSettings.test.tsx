import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {MemoryRouter} from "react-router-dom";
import {beforeEach, describe, expect, it, vi} from "vitest";
import type {SshEnvironment, SshEnvironmentDiscovery, SshEnvironmentHost} from "../api/types";
import {DialogProvider} from "./DialogSystem";
import {EnvironmentSettings, hostStatus} from "./EnvironmentSettings";

const api = {discoverSshEnvironments: vi.fn(), saveSshEnvironment: vi.fn(), probeSshEnvironment: vi.fn(), forgetSshEnvironment: vi.fn()};
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api, previewMode: false})}));
vi.mock("../diagnostics", () => ({logCaughtDiagnostic: vi.fn(), DiagnosticErrorNotice: ({error}: {error: string}) => <div role="alert">{error}</div>}));

function environment(alias: string, overrides: Partial<SshEnvironment> = {}): SshEnvironment {
  return {id: `ssh:${alias}`, revision: 1, alias, displayName: "", label: alias, enabled: false, notes: "", commandApproval: "ask", ...overrides};
}

const reachable = {status: "reachable" as const, latencyMs: 38, system: "Darwin", osVersion: "macOS 27.0", arch: "arm64", model: "Mac17,5", tools: ["python3", "git"], passwordlessSudo: false, detail: "", checkedAt: "2026-09-19T12:00:00Z"};

function host(alias: string, extra: Partial<SshEnvironmentHost> = {}): SshEnvironmentHost {
  return {alias, aliases: [alias], comment: "", inConfig: true, resolved: {hostname: `${alias}.local`, user: "research", port: 22, identityFiles: ["~/.ssh/id_ed25519"], options: {}}, ...extra};
}

function discovery(hosts: SshEnvironmentHost[]): SshEnvironmentDiscovery {
  return {configPath: "/home/agent/.ssh/config", configExists: true, sshAvailable: true, filesRead: [], skippedPatterns: ["*"], skippedMatchBlocks: 0, errors: [], hosts, readAt: new Date().toISOString()};
}

function renderSettings() {
  return render(<MemoryRouter><DialogProvider><EnvironmentSettings /></DialogProvider></MemoryRouter>);
}

describe("environment settings", () => {
  beforeEach(() => vi.resetAllMocks());

  it("lists every config host before any is set up, and says nothing is enabled", async () => {
    api.discoverSshEnvironments.mockResolvedValue(discovery([host("research3"), host("jetson")]));
    renderSettings();

    const list = await screen.findByRole("list", {name: "SSH hosts"});
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText(/2 hosts · 0 enabled/)).toBeVisible();
    expect(screen.getByText("Skipped: Host *")).toBeVisible();
    expect(within(list).getAllByRole("switch").every((item) => !(item as HTMLInputElement).checked)).toBe(true);
  });

  it("enables a host, runs its first connection test, and shows the result in the row", async () => {
    api.discoverSshEnvironments.mockResolvedValue(discovery([host("research3")]));
    api.saveSshEnvironment.mockResolvedValue(environment("research3", {enabled: true}));
    api.probeSshEnvironment.mockResolvedValue(environment("research3", {enabled: true, revision: 2, lastProbe: reachable}));
    const user = userEvent.setup();
    renderSettings();

    await user.click(await screen.findByRole("switch", {name: "Use research3 from Nebula"}));

    await waitFor(() => expect(screen.getByText("Reachable · 38 ms")).toBeVisible());
    expect(api.saveSshEnvironment).toHaveBeenCalledWith("research3", {enabled: true}, undefined);
    expect(api.probeSshEnvironment).toHaveBeenCalledWith("research3");
    expect(screen.getByText("macOS 27.0")).toBeVisible();
    expect(screen.getByRole("switch", {name: "Use research3 from Nebula"})).toBeChecked();
  });

  it("keeps every row in place when the first host is enabled", async () => {
    api.discoverSshEnvironments.mockResolvedValue(discovery([host("research3"), host("jetson")]));
    api.saveSshEnvironment.mockResolvedValue(environment("research3", {enabled: true, lastProbe: reachable}));
    const user = userEvent.setup();
    renderSettings();

    await user.click(await screen.findByRole("switch", {name: "Use research3 from Nebula"}));

    await waitFor(() => expect(screen.getByText("Reachable · 38 ms")).toBeVisible());
    expect(within(screen.getByRole("list", {name: "SSH hosts"})).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByRole("button", {name: /more host/})).not.toBeInTheDocument();
  });

  it("folds untouched hosts once one is configured", async () => {
    api.discoverSshEnvironments.mockResolvedValue(discovery([host("research3", {environment: environment("research3", {enabled: true})}), host("jetson"), host("iMac")]));
    const user = userEvent.setup();
    renderSettings();

    const more = await screen.findByRole("button", {name: /2 more hosts in ~\/.ssh\/config/});
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(within(screen.getByRole("list", {name: "SSH hosts"})).getAllByRole("listitem")).toHaveLength(1);
    await user.click(more);
    expect(within(screen.getByRole("list", {name: "Other SSH hosts"})).getAllByRole("listitem")).toHaveLength(2);
  });

  it("saves Nebula settings from the details dialog with the current revision", async () => {
    api.discoverSshEnvironments.mockResolvedValue(discovery([host("research3", {comment: "Apple Silicon research Mac", environment: environment("research3", {enabled: true, revision: 4, lastProbe: reachable})})]));
    api.saveSshEnvironment.mockImplementation(async (_alias, change) => environment("research3", {enabled: true, revision: 5, displayName: change.displayName, label: change.displayName, commandApproval: change.commandApproval}));
    const user = userEvent.setup();
    renderSettings();

    await user.click(await screen.findByRole("button", {name: "research3 details"}));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("research3.local")).toBeVisible();
    expect(within(dialog).getByRole("textbox", {name: /Notes for agents/})).toHaveValue("Apple Silicon research Mac");
    expect(within(dialog).getByText(/Comments directly below its/)).toHaveTextContent("Host line are included automatically");
    await user.type(within(dialog).getByRole("textbox", {name: "Display name"}), "Research Mac");
    await user.selectOptions(within(dialog).getByRole("combobox", {name: "Command approval"}), "allow");
    await user.click(within(dialog).getByRole("button", {name: "Save"}));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.saveSshEnvironment).toHaveBeenCalledWith("research3", expect.objectContaining({displayName: "Research Mac", commandApproval: "allow", enabled: true}), 4);
    expect(screen.getByRole("button", {name: "Research Mac details"})).toBeVisible();
  });

  it("explains how to recover from an untrusted host key", async () => {
    api.discoverSshEnvironments.mockResolvedValue(discovery([host("pi-one", {environment: environment("pi-one", {lastProbe: {...reachable, status: "host_key_untrusted"}})})]));
    renderSettings();

    expect(await screen.findByText("Host key not trusted")).toBeVisible();
    expect(screen.getByText(/Connect once with ssh pi-one on the Nebula host/)).toBeVisible();
  });

  it("shows an empty state when the config has no hosts", async () => {
    api.discoverSshEnvironments.mockResolvedValue(discovery([]));
    renderSettings();

    expect(await screen.findByText("No SSH hosts")).toBeVisible();
  });
});

describe("hostStatus", () => {
  it("flags hosts removed from the config", () => {
    expect(hostStatus(host("gone", {inConfig: false, resolved: undefined}))).toEqual({label: "Removed from ~/.ssh/config", tone: "warning"});
  });
});

describe("host details dialog", () => {
  beforeEach(() => vi.resetAllMocks());

  it("shows a failed connection test inside the dialog", async () => {
    api.discoverSshEnvironments.mockResolvedValue(discovery([host("research3", {environment: environment("research3", {enabled: true, revision: 4})})]));
    api.probeSshEnvironment.mockRejectedValue(new Error("Permission denied (publickey)."));
    const user = userEvent.setup();
    renderSettings();

    await user.click(await screen.findByRole("button", {name: "research3 details"}));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", {name: "Test connection"}));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Permission denied (publickey).");
    expect(screen.getByRole("dialog")).toBe(dialog);
  });

  it("keeps the dialog open while a save is in flight", async () => {
    api.discoverSshEnvironments.mockResolvedValue(discovery([host("research3")]));
    let finishSave!: (saved: SshEnvironment) => void;
    api.saveSshEnvironment.mockImplementation(() => new Promise<SshEnvironment>((resolve) => { finishSave = resolve; }));
    const user = userEvent.setup();
    renderSettings();

    await user.click(await screen.findByRole("button", {name: "research3 details"}));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", {name: "Save"}));

    expect(within(dialog).getByRole("button", {name: "Saving…"})).toBeDisabled();
    expect(within(dialog).getByRole("button", {name: "Close host details"})).toBeDisabled();
    expect(within(dialog).getByRole("button", {name: "Cancel"})).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBe(dialog);

    finishSave(environment("research3", {revision: 2}));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
