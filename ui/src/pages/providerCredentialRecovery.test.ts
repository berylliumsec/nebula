import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { CredentialStatus } from "../api/types";
import { notSentMessage } from "./chatToolResultConsent";
import {
  prepareProviderCredential,
  runWithProviderCredentialRecovery,
} from "./providerCredentialRecovery";

async function refusal(status: unknown, unlock = vi.fn()): Promise<string> {
  const credentialStatus = vi.fn().mockResolvedValue(status);
  try {
    await prepareProviderCredential({ credentialStatus } as never, "vault:abc", unlock);
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
  throw new Error("the credential check accepted the send");
}

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

  it("explains every state Core reports with the next valid action, short enough to read whole", async () => {
    const states: Array<[CredentialStatus["persistence"], CredentialStatus["state"], string]> = [
      ["vault", "locked", "Unlock it on the host"],
      ["vault", "missing", "Replace it in Settings"],
      ["vault", "backend_unavailable", "Start it on the host or replace the credential in Settings"],
      ["session", "session_expired", "Replace it in Settings"],
      ["environment", "environment_missing", "Set it and restart Core"],
      ["systemd", "service_credential_missing", "Check LoadCredential"],
    ];
    for (const [persistence, state, action] of states) {
      const message = await refusal(
        { reference: "vault:abc", persistence, available: false, state },
        vi.fn().mockRejectedValue(new Error("Unlock the credential vault on the Nebula Core host, then retry.")),
      );
      expect(message, state).toContain(state === "locked" ? "Unlock" : action);
      expect(message.trim(), state).not.toBe("");
      expect(notSentMessage(message, true).length, state).toBeLessThanOrEqual(180);
    }
  });

  it("names a credential state this interface does not know instead of dropping the send", async () => {
    const message = await refusal({ reference: "vault:abc", persistence: "vault", available: false, state: "rotating" });
    expect(message).toBe('Nebula Core reported an unknown credential state "rotating". Check this provider in Settings, then send again.');
    // An unreadable name is left out rather than echoed.
    expect(await refusal({ reference: "vault:abc", persistence: "vault", available: false, state: "<script>".repeat(10) }))
      .toBe("Nebula Core reported an unknown credential state. Check this provider in Settings, then send again.");
    // Core never pairs these; treat the pair as unknown, never as "available".
    expect(await refusal({ reference: "vault:abc", persistence: "vault", available: false, state: "available" }))
      .toContain('unknown credential state "available"');
  });

  it("refuses an unreadable status answer with a clear next action", async () => {
    for (const answer of [[], {}, null, { available: "yes", state: "available" }]) {
      expect(await refusal(answer)).toBe(
        "Nebula Core returned an unreadable credential status for this provider. Check this provider in Settings, then send again.",
      );
    }
  });

  it("separates an unreachable Core from a refused credential check", async () => {
    const unreachable = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(prepareProviderCredential({ credentialStatus: unreachable } as never, "env:KEY", vi.fn()))
      .rejects.toThrow("Could not reach Nebula Core to check this provider's credential. Send again once Core responds.");
    const refused = vi.fn().mockRejectedValue(new ApiError("credential reference is invalid", 422, undefined, { error_id: "err_ref" }));
    await expect(prepareProviderCredential({ credentialStatus: refused } as never, "env:KEY", vi.fn()))
      .rejects.toThrow("Nebula Core could not check this provider's credential. Replace it in Settings, then send again. Reference: err_ref.");
  });

  it("keeps the vault guidance when a host unlock fails without a reason", async () => {
    const message = await refusal(
      { reference: "vault:abc", persistence: "vault", available: false, state: "locked" },
      vi.fn().mockRejectedValue("denied"),
    );
    expect(message).toBe("The credential vault on the Nebula Core host is locked. Unlock it on the host, then send again.");
  });
});

