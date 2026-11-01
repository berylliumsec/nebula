import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {ButlerCredentialCreate} from "./ButlerCredentialCreate";

const reference = `session:${"a".repeat(32)}`;
const status = {reference, persistence: "session" as const, available: true, state: "available" as const};

describe("Butler write-only credential entry", () => {
  it("cancels without sending a secret to Core", async () => {
    const api = {credentialVaultStatus: vi.fn().mockResolvedValue({state: "locked", available: false}),
      createCredential: vi.fn(), credentialStatus: vi.fn()} as unknown as ApiClient;
    const onCancel = vi.fn();
    render(<ButlerCredentialCreate api={api} projectId="project" currentProjectId={() => "project"}
      onSaved={vi.fn()} onUncertain={vi.fn()} onCancel={onCancel} />);
    fireEvent.change(screen.getByLabelText("Secret"), {target: {value: "private-key"}});
    fireEvent.click(screen.getByRole("button", {name: "Cancel"}));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(api.createCredential).not.toHaveBeenCalled();
  });

  it("saves on this browser and reports only a read-backed reference", async () => {
    const api = {credentialVaultStatus: vi.fn().mockResolvedValue({state: "locked", available: false}),
      createCredential: vi.fn().mockResolvedValue(status), credentialStatus: vi.fn().mockResolvedValue(status)} as unknown as ApiClient;
    const onSaved = vi.fn();
    render(<ButlerCredentialCreate api={api} projectId="project" currentProjectId={() => "project"}
      onSaved={onSaved} onUncertain={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Secret"), {target: {value: "private-key"}});
    fireEvent.click(screen.getByRole("button", {name: "Save credential"}));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(reference, "session"));
    expect(api.createCredential).toHaveBeenCalledWith("private-key", "session");
    expect(api.credentialStatus).toHaveBeenCalledWith(reference);
    expect(screen.getByLabelText("Secret")).toHaveValue("");
    expect(JSON.stringify(onSaved.mock.calls)).not.toContain("private-key");
  });

  it("does not resubmit after Core issued a reference but readback failed", async () => {
    const api = {credentialVaultStatus: vi.fn().mockResolvedValue({state: "locked", available: false}),
      createCredential: vi.fn().mockResolvedValue(status), credentialStatus: vi.fn().mockRejectedValue(new Error("offline"))} as unknown as ApiClient;
    const onUncertain = vi.fn();
    render(<ButlerCredentialCreate api={api} projectId="project" currentProjectId={() => "project"}
      onSaved={vi.fn()} onUncertain={onUncertain} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Secret"), {target: {value: "private-key"}});
    fireEvent.click(screen.getByRole("button", {name: "Save credential"}));
    await waitFor(() => expect(onUncertain).toHaveBeenCalledWith(reference));
    expect(api.createCredential).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(onUncertain.mock.calls)).not.toContain("private-key");
  });
});
