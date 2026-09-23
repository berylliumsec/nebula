import { ApiError, type ApiClient } from "../api/client";
import type { CredentialStatus } from "../api/types";
import { unlockProviderCredential } from "../api/runtime";

type CredentialStatusClient = Pick<ApiClient, "credentialStatus">;
type UnlockCredential = (reference: string) => Promise<void>;

function unavailableCredentialMessage(status: CredentialStatus): string {
  switch (status.state) {
    case "locked":
      return "The credential vault is still locked. Unlock it on the Nebula Core host, then retry.";
    case "missing":
      return "This provider credential no longer exists. Replace it in Settings, then retry.";
    case "backend_unavailable":
      return "The operating-system credential vault is unavailable on the Nebula Core host.";
    case "session_expired":
      return "This session-only provider credential expired. Replace it in Settings, then retry.";
    case "environment_missing":
      return "The configured credential environment variable is not available to Nebula Core.";
    case "service_credential_missing":
      return "The configured systemd service credential is not available to Nebula Core. Check LoadCredential for the Core service, then retry.";
    case "available":
      return "The provider credential is available.";
  }
}

/**
 * Checks a provider's opaque credential reference before accepting a chat turn.
 * A local desktop can ask Secret Service to unlock a vault item; browsers and
 * remote desktops receive host-specific guidance from the native boundary.
 */
export async function prepareProviderCredential(
  api: CredentialStatusClient,
  reference: string | undefined,
  unlock: UnlockCredential = unlockProviderCredential,
): Promise<void> {
  const normalized = reference?.trim();
  if (!normalized) return;

  let status = await api.credentialStatus(normalized);
  if (status.available) return;

  if (status.persistence === "vault" && status.state === "locked") {
    await unlock(normalized);
    status = await api.credentialStatus(normalized);
    if (status.available) return;
  }

  throw new Error(unavailableCredentialMessage(status));
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
