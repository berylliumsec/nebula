import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {deleteButlerCredential} from "./butlerCredentialAction";

const reference = `session:${"a".repeat(32)}`;
const action = {kind: "credential_delete" as const, projectId: "project", reference};
const available = {reference, persistence: "session" as const, available: true, state: "available" as const};

describe("Butler credential deletion", () => {
  it("requires the original project and a current exact managed reference", async () => {
    const api = {credentialStatus: vi.fn().mockResolvedValue({...available, reference: "other"}),
      deleteCredential: vi.fn()} as unknown as ApiClient;
    await expect(deleteButlerCredential(api, action, () => "other", vi.fn()))
      .rejects.toThrow("original project");
    expect(api.credentialStatus).not.toHaveBeenCalled();
    await expect(deleteButlerCredential(api, action, () => "project", vi.fn()))
      .rejects.toThrow("unavailable or changed");
    expect(api.deleteCredential).not.toHaveBeenCalled();
  });

  it("cancellation leaves the credential available", async () => {
    const api = {credentialStatus: vi.fn().mockResolvedValue(available),
      deleteCredential: vi.fn()} as unknown as ApiClient;
    expect(await deleteButlerCredential(api, action, () => "project", async () => false))
      .toContain("was not deleted");
    expect(api.deleteCredential).not.toHaveBeenCalled();
  });

  it("deletes after a second status check and confirms a session reference expired", async () => {
    const api = {credentialStatus: vi.fn().mockResolvedValueOnce(available)
      .mockResolvedValueOnce(available).mockResolvedValueOnce({...available, available: false, state: "session_expired"}),
      deleteCredential: vi.fn().mockResolvedValue(undefined)} as unknown as ApiClient;
    const result = await deleteButlerCredential(api, action, () => "project", async () => true);
    expect(api.deleteCredential).toHaveBeenCalledWith(reference);
    expect(result).toContain("Core confirms it is unavailable");
  });

  it("returns an uncertain receipt if readback cannot prove absence", async () => {
    const api = {credentialStatus: vi.fn().mockResolvedValue(available),
      deleteCredential: vi.fn().mockResolvedValue(undefined)} as unknown as ApiClient;
    const result = await deleteButlerCredential(api, action, () => "project", async () => true);
    expect(result).toContain("could not be confirmed");
  });
});
