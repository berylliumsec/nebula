import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import {
  prepareProviderCredential,
  runWithProviderCredentialRecovery,
} from "./providerCredentialRecovery";

describe("provider credential recovery", () => {
  it("unlocks a locked vault credential and verifies it before continuing", async () => {
    const credentialStatus = vi.fn()
      .mockResolvedValueOnce({ reference: "vault:abc", persistence: "vault", available: false, state: "locked" })
      .mockResolvedValueOnce({ reference: "vault:abc", persistence: "vault", available: true, state: "available" });
    const unlock = vi.fn().mockResolvedValue(undefined);

    await prepareProviderCredential({ credentialStatus } as never, "vault:abc", unlock);

    expect(unlock).toHaveBeenCalledWith("vault:abc");
    expect(credentialStatus).toHaveBeenCalledTimes(2);
  });

  it("does not expose or unlock a missing service credential", async () => {
    const credentialStatus = vi.fn().mockResolvedValue({
      reference: "systemd:openai-api-key",
      persistence: "systemd",
      available: false,
      state: "service_credential_missing",
    });
    const unlock = vi.fn();

    await expect(prepareProviderCredential(
      { credentialStatus } as never,
      "systemd:openai-api-key",
      unlock,
    )).rejects.toThrow("Check LoadCredential for the Core service");
    expect(unlock).not.toHaveBeenCalled();
  });

  it("unlocks and retries once when Core rejects the turn before acceptance", async () => {
    const credentialStatus = vi.fn()
      .mockResolvedValueOnce({ reference: "vault:abc", persistence: "vault", available: false, state: "locked" })
      .mockResolvedValueOnce({ reference: "vault:abc", persistence: "vault", available: true, state: "available" });
    const unlock = vi.fn().mockResolvedValue(undefined);
    const request = vi.fn()
      .mockRejectedValueOnce(new ApiError("Vault locked", 503, undefined, { code: "provider_credential_locked" }))
      .mockResolvedValueOnce("completed");

    await expect(runWithProviderCredentialRecovery(
      { credentialStatus } as never,
      "vault:abc",
      request,
      unlock,
    )).resolves.toBe("completed");
    expect(request).toHaveBeenCalledTimes(2);
    expect(unlock).toHaveBeenCalledTimes(1);
  });
});
