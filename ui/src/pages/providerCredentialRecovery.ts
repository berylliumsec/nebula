import { ApiError, type ApiClient } from "../api/client";
import type { CredentialStatus } from "../api/types";
import { unlockProviderCredential } from "../api/runtime";

type CredentialStatusClient = Pick<ApiClient, "credentialStatus">;
type UnlockCredential = (reference: string) => Promise<void>;

const LOCKED_MESSAGE = "The credential vault on the Nebula Core host is locked. Unlock it on the host, then send again.";

// Every state Core's CredentialStatus can report (credentials.py). A state
// outside this list comes from a Core this interface does not know yet.
const KNOWN_STATES: ReadonlySet<string> = new Set<CredentialStatus["state"]>([
  "available",
  "locked",
  "missing",
  "backend_unavailable",
  "session_expired",
  "environment_missing",
  "service_credential_missing",
]);

function unavailableCredentialMessage(status: CredentialStatus): string {
  switch (status.state) {
    case "locked":
      return LOCKED_MESSAGE;
    case "missing":
      return "This provider's saved credential no longer exists. Replace it in Settings, then send again.";
    case "backend_unavailable":
      return "The credential vault on the Nebula Core host is unavailable. Start it on the host or replace the credential in Settings, then send again.";
    case "session_expired":
      return "This session-only provider credential ended when Nebula Core restarted. Replace it in Settings, then send again.";
    case "environment_missing":
      return "The provider's credential environment variable is not set for Nebula Core. Set it and restart Core, or replace it in Settings, then send again.";
    case "service_credential_missing":
      return "The configured systemd service credential is not available to Nebula Core. Check LoadCredential for the Core service, then send again.";
    case "available":
      // Core never reports an unavailable credential in this state.
      return unrecognisedStatusMessage("available");
  }
}

function unrecognisedStatusMessage(state: unknown): string {
  const named = typeof state === "string" && /^[a-z0-9_.-]{1,32}$/i.test(state) ? ` "${state}"` : "";
  return `Nebula Core reported an unknown credential state${named}. Check this provider in Settings, then send again.`;
}

function readableStatus(value: unknown): value is CredentialStatus {
  if (!value || typeof value !== "object") return false;
  const status = value as Partial<CredentialStatus>;
  return typeof status.available === "boolean" && typeof status.state === "string";
}

function statusCheckFailure(error: unknown): Error {
  const reference = error instanceof ApiError ? error.errorId ?? error.requestId : undefined;
  const suffix = reference ? ` Reference: ${reference}.` : "";
  // A refusal (4xx) is Core's answer about this reference; anything else
  // means Core could not answer, and sending again later can succeed.
  const message = error instanceof ApiError && error.status >= 400 && error.status < 500
    ? "Nebula Core could not check this provider's credential. Replace it in Settings, then send again."
    : "Could not reach Nebula Core to check this provider's credential. Send again once Core responds.";
  return new Error(`${message}${suffix}`, { cause: error });
}

async function readStatus(api: CredentialStatusClient, reference: string): Promise<CredentialStatus> {
  let status: unknown;
  try {
    status = await api.credentialStatus(reference);
  } catch (error) {
    // diagnostic-expected: the client logged the failed request; the send stops with this explanation.
    throw statusCheckFailure(error);
  }
  if (!readableStatus(status)) {
    throw new Error("Nebula Core returned an unreadable credential status for this provider. Check this provider in Settings, then send again.");
  }
  return status;
}

/**
 * Checks a provider's opaque credential reference before accepting a chat turn.
 * A local desktop can ask Secret Service to unlock a vault item; browsers and
 * remote desktops receive host-specific guidance from the native boundary.
 * Every refusal names what failed and the next valid action, including a state
 * this interface does not recognise.
 */
export async function prepareProviderCredential(
  api: CredentialStatusClient,
  reference: string | undefined,
  unlock: UnlockCredential = unlockProviderCredential,
): Promise<void> {
  const normalized = reference?.trim();
  if (!normalized) return;

  let status = await readStatus(api, normalized);
  if (status.available) return;

  if (status.persistence === "vault" && status.state === "locked") {
    try {
      await unlock(normalized);
    } catch (error) {
      // diagnostic-expected: an unlock the host must perform is guidance, not a fault.
      throw new Error(error instanceof Error && error.message ? error.message : LOCKED_MESSAGE, { cause: error });
    }
    status = await readStatus(api, normalized);
    if (status.available) return;
  }

  throw new Error(KNOWN_STATES.has(status.state)
    ? unavailableCredentialMessage(status)
    : unrecognisedStatusMessage(status.state));
}

/** Retry once only for Core's pre-acceptance locked-vault response. */
export async function runWithProviderCredentialRecovery<T>(
  api: CredentialStatusClient,
  reference: string | undefined,
  request: () => Promise<T>,
  unlock: UnlockCredential = unlockProviderCredential,
): Promise<T> {
  try {
    return await request();
  } catch (error) {
    if (
      !reference?.startsWith("vault:")
      || !(error instanceof ApiError)
      || error.code !== "provider_credential_locked"
    ) {
      throw error;
    }
    await prepareProviderCredential(api, reference, unlock);
    return request();
  }
}
