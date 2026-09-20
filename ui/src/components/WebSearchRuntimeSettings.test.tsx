import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {MemoryRouter} from "react-router-dom";
import {beforeEach, describe, expect, it, vi} from "vitest";
import type {WebSearchRuntime} from "../api/types";
import {DialogProvider} from "./DialogSystem";
import {WebSearchRuntimeSettings, runtimeHealth, shortDigest} from "./WebSearchRuntimeSettings";

const api = {
  getWebSearchRuntime: vi.fn(),
  installWebSearchRuntime: vi.fn(),
  startWebSearchRuntime: vi.fn(),
  stopWebSearchRuntime: vi.fn(),
  testWebSearchRuntime: vi.fn(),
  setWebSearchEngines: vi.fn(),
  removeWebSearchRuntime: vi.fn(),
};
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api, coreState: "online", previewMode: false})}));
vi.mock("../diagnostics", () => ({logCaughtDiagnostic: vi.fn(), DiagnosticErrorNotice: ({error}: {error: unknown}) => <div role="alert">{String(error)}</div>}));
vi.mock("./SettingsSaveFeedback", () => ({announceSettingsSaved: vi.fn()}));

const digest = `sha256:${"9f2c1a".padEnd(64, "0")}`;

function runtime(changes: Partial<WebSearchRuntime> = {}): WebSearchRuntime {
  return {
    runtimeAvailable: true,
    runtimeDetail: "the search runtime is running",
    containerState: "ready",
    imageDigest: digest,
    port: 24800,
    engines: ["duckduckgo", "brave"],
    projectsUsing: 2,
    ...changes,
  };
}

function mount() {
  render(<MemoryRouter><DialogProvider><WebSearchRuntimeSettings /></DialogProvider></MemoryRouter>);
}

describe("web search runtime settings", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.getWebSearchRuntime.mockResolvedValue(runtime());
  });

  it("shows a ready runtime with its pinned image and loopback address", async () => {
    mount();
    expect(await screen.findByRole("status")).toHaveTextContent("Ready");
    expect(screen.getByText(/searxng\/searxng/)).toBeInTheDocument();
    expect(screen.getByText("127.0.0.1:24800 · loopback only")).toBeInTheDocument();
    expect(screen.getByText("2 projects")).toBeInTheDocument();
  });

  it("states there is no key to manage", async () => {
    mount();
    await screen.findByRole("status");
    expect(screen.getByText(/No account and no API key/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/key/i)).not.toBeInTheDocument();
  });

  it("warns that queries still leave the machine", async () => {
    mount();
    await screen.findByRole("status");
    expect(screen.getByText(/Queries reach the engines you select/)).toBeInTheDocument();
    expect(screen.getByText(/local.only cannot use web search/i)).toBeInTheDocument();
  });

  it("offers Install when no image is pinned yet", async () => {
    api.getWebSearchRuntime.mockResolvedValue(runtime({imageDigest: undefined, containerState: "absent"}));
    api.installWebSearchRuntime.mockResolvedValue(runtime());
    const user = userEvent.setup();
    mount();
    await screen.findByRole("status");
    expect(screen.queryByRole("button", {name: "Start"})).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", {name: "Install"}));
    await waitFor(() => expect(api.installWebSearchRuntime).toHaveBeenCalledTimes(1));
  });

  it("offers Start for an installed but stopped runtime", async () => {
    api.getWebSearchRuntime.mockResolvedValue(runtime({containerState: "stopped"}));
    api.startWebSearchRuntime.mockResolvedValue(runtime());
    const user = userEvent.setup();
    mount();
    await screen.findByRole("status");
    expect(screen.queryByRole("button", {name: "Stop"})).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", {name: "Start"}));
    await waitFor(() => expect(api.startWebSearchRuntime).toHaveBeenCalledTimes(1));
  });

  it("explains a missing container runtime instead of offering dead controls", async () => {
    api.getWebSearchRuntime.mockResolvedValue(runtime({
      runtimeAvailable: false,
      runtimeDetail: "web search needs an enabled, verified container runtime",
      containerState: "absent",
      imageDigest: undefined,
    }));
    mount();
    expect(await screen.findByRole("status")).toHaveTextContent("No container runtime");
    expect(screen.getByText(/needs an enabled, verified container runtime/)).toBeInTheDocument();
  });

  it("saves an engine change and keeps the last engine selected", async () => {
    api.setWebSearchEngines.mockResolvedValue(runtime({engines: ["duckduckgo", "brave", "wikipedia"]}));
    const user = userEvent.setup();
    mount();
    await screen.findByRole("status");
    await user.click(screen.getByRole("checkbox", {name: /Wikipedia/}));
    await waitFor(() => expect(api.setWebSearchEngines).toHaveBeenCalledWith(["duckduckgo", "brave", "wikipedia"]));
  });

  it("disables the only remaining engine so a project cannot end up with none", async () => {
    api.getWebSearchRuntime.mockResolvedValue(runtime({engines: ["duckduckgo"]}));
    mount();
    await screen.findByRole("status");
    expect(screen.getByRole("checkbox", {name: /DuckDuckGo/})).toBeDisabled();
  });

  it("reports a failed test with its reason", async () => {
    api.getWebSearchRuntime.mockResolvedValue(runtime({
      lastTest: {testedAt: new Date().toISOString(), ok: false, enginesAnswered: [], error: "the search runtime is not installed"},
    }));
    mount();
    await screen.findByRole("status");
    expect(screen.getByText(/the search runtime is not installed/)).toBeInTheDocument();
  });

  it("surfaces a failed operation without clearing the panel", async () => {
    api.testWebSearchRuntime.mockRejectedValue(new Error("runtime unreachable"));
    const user = userEvent.setup();
    mount();
    await screen.findByRole("status");
    await user.click(screen.getByRole("button", {name: "Test"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("runtime unreachable");
    expect(screen.getByText("127.0.0.1:24800 · loopback only")).toBeInTheDocument();
  });
});

describe("runtime health and digest helpers", () => {
  it("maps each container state to an operator-readable status", () => {
    expect(runtimeHealth(undefined).label).toBe("Unknown");
    expect(runtimeHealth(runtime({runtimeAvailable: false})).label).toBe("No container runtime");
    expect(runtimeHealth(runtime({containerState: "ready"}))).toEqual({label: "Ready", tone: "good"});
    expect(runtimeHealth(runtime({containerState: "failed"}))).toEqual({label: "Failed", tone: "bad"});
    expect(runtimeHealth(runtime({containerState: "absent"})).label).toBe("Not installed");
  });

  it("shortens a pinned digest and leaves a short one alone", () => {
    expect(shortDigest(digest)).toBe("sha256:9f2c1a…0000");
    expect(shortDigest(undefined)).toBeUndefined();
    expect(shortDigest("sha256:abc")).toBe("sha256:abc");
  });
});
