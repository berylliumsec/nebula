import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {MemoryRouter} from "react-router-dom";
import {beforeEach, describe, expect, it, vi} from "vitest";
import type {HarnessProfile} from "../api/types";
import {DialogProvider} from "./DialogSystem";
import {HarnessSettings} from "./HarnessSettings";

const profile: HarnessProfile = {
  id: "saved", name: "Local fixture", kind: "grok_acp", connectionMode: "spawn", transport: "stdio",
  executable: "/disposable/inert-fixture", authMode: "existing_session", models: ["fixture-model"],
  enabled: true, localOnly: true, permitsSensitiveData: false, revision: 1,
  nativeCapabilities: {workspaceAccess: "none", shell: false, webSearch: false, webFetch: false, browser: false, computerUse: false, imageGeneration: false, skills: false, subagents: false},
};
const api = {listHarnesses: vi.fn(), listMcpServers: vi.fn(), createHarness: vi.fn(), checkHarness: vi.fn()};
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
