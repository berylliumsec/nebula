import {render, screen, waitFor} from "@testing-library/react";
import {beforeEach, describe, expect, it, vi} from "vitest";
import {useCredentialVault, vaultUnavailableNote} from "./useCredentialVault";

const fixture = vi.hoisted(() => ({api: {} as Record<string, ReturnType<typeof vi.fn>>}));
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api: fixture.api, coreState: "online"})}));
vi.mock("../diagnostics", () => ({logCaughtDiagnostic: vi.fn()}));

function Probe() {
  const vault = useCredentialVault();
  return <p>{`${vault.state} · ${vault.available}`}</p>;
}

describe("credential vault state", () => {
  beforeEach(() => { fixture.api = {}; });

  it("reports the lock state Core sends", async () => {
    fixture.api = {credentialVaultStatus: vi.fn(async () => ({state: "locked", available: false}))};
    render(<Probe />);
    expect(await screen.findByText("locked · false")).toBeInTheDocument();
  });

  it("leaves the vault on offer when Core cannot answer", async () => {
    fixture.api = {credentialVaultStatus: vi.fn(async () => { throw new Error("older Core"); })};
    render(<Probe />);
    await waitFor(() => expect(fixture.api.credentialVaultStatus).toHaveBeenCalled());
    expect(screen.getByText("available · true")).toBeInTheDocument();
  });

  it("separates a locked vault from an absent one in the dialog note", () => {
    expect(vaultUnavailableNote("available")).toBeUndefined();
    expect(vaultUnavailableNote("locked", "key")).toMatch(/locked.*Unlock the keyring on the Core host/);
    expect(vaultUnavailableNote("unavailable", "key")).toMatch(/unavailable/);
  });
});
