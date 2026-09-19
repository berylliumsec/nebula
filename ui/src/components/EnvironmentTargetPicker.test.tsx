import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {MemoryRouter} from "react-router-dom";
import {beforeEach, describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import type {SshEnvironment} from "../api/types";
import {EnvironmentTargetPicker, environmentIdsForTarget} from "./EnvironmentTargetPicker";

vi.mock("../diagnostics", () => ({logCaughtDiagnostic: vi.fn()}));

const researchMac: SshEnvironment = {
  id: "ssh:research3", revision: 2, alias: "research3", displayName: "Research Mac", label: "Research Mac", enabled: true, notes: "", commandApproval: "ask",
  lastProbe: {status: "reachable", latencyMs: 38, system: "Darwin", osVersion: "macOS 27.0", arch: "arm64", model: "Mac17,5", tools: [], detail: "", checkedAt: "2026-09-19T12:00:00Z"},
};
const discover = vi.fn();
const api = {discoverSshEnvironments: discover} as unknown as ApiClient;

function hosts(...environments: Array<SshEnvironment | undefined>) {
  return {hosts: environments.map((environment, index) => ({alias: environment?.alias ?? `plain-${index}`, aliases: [], comment: "", inConfig: true, environment}))};
}

describe("environment target picker", () => {
  beforeEach(() => vi.resetAllMocks());

  it("maps targets to the chat request field", () => {
    expect(environmentIdsForTarget("auto")).toBeUndefined();
    expect(environmentIdsForTarget("core")).toEqual([]);
    expect(environmentIdsForTarget("ssh:research3")).toEqual(["ssh:research3"]);
  });

  it("renders nothing until a host is enabled", async () => {
    discover.mockResolvedValue(hosts(undefined, {...researchMac, enabled: false}));
    const {container} = render(<MemoryRouter><EnvironmentTargetPicker api={api} value="auto" onChange={vi.fn()} /></MemoryRouter>);

    await waitFor(() => expect(discover).toHaveBeenCalledWith(expect.anything(), {resolve: false}));
    expect(container).toBeEmptyDOMElement();
  });

  it("pins a host from the menu and links to settings", async () => {
    discover.mockResolvedValue(hosts(researchMac));
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<MemoryRouter><EnvironmentTargetPicker api={api} value="auto" onChange={onChange} /></MemoryRouter>);

    await user.click(await screen.findByRole("button", {name: "Where commands run: Auto"}));
    expect(screen.getByRole("button", {name: /^Auto/})).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("macOS 27.0 · arm64 · 38 ms")).toBeVisible();
    expect(screen.getByRole("link", {name: "Manage environments…"})).toHaveAttribute("href", "/settings#ssh-environment-settings");
    await user.click(screen.getByRole("button", {name: /^Research Mac/}));

    expect(onChange).toHaveBeenCalledWith("ssh:research3");
    expect(screen.queryByRole("group", {name: "Where should commands run?"})).not.toBeInTheDocument();
  });

  it("falls back to Auto when the pinned host is no longer enabled", async () => {
    discover.mockResolvedValue(hosts({...researchMac, id: "ssh:other", alias: "other"}));
    const onChange = vi.fn();
    render(<MemoryRouter><EnvironmentTargetPicker api={api} value="ssh:research3" onChange={onChange} /></MemoryRouter>);

    await waitFor(() => expect(onChange).toHaveBeenCalledWith("auto"));
  });
});
