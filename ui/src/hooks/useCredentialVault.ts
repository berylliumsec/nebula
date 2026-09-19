import { useCallback, useEffect, useState } from "react";
import type { VaultState } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { useWorkspace } from "../state/WorkspaceContext";

/**
 * Why a secret cannot reach the operating-system vault, for a dialog note.
 *
 * A Linux vault is normally present but locked when Core runs headless, and a
 * save into a locked vault always fails, so the dialog offers session storage
 * and names the remedy instead.
 */
export function vaultUnavailableNote(state: VaultState, subject = "secret"): string | undefined {
  if (state === "available") return undefined;
  if (state === "locked") {
    return `The host credential vault is locked, so the ${subject} is kept for this Nebula session only. Unlock the keyring on the Core host to store it in the vault.`;
  }
  return `The operating-system credential vault is unavailable, so the ${subject} is kept for this Nebula session only.`;
}

/** The vault state Core reports, so a dialog never offers a save that fails. */
export function useCredentialVault(): { state: VaultState; available: boolean; refresh: () => void } {
  const { api, coreState } = useWorkspace();
  // Optimistic until Core answers: an unreachable Core changes no dialog.
  const [state, setState] = useState<VaultState>("available");

  const refresh = useCallback(() => {
    if (!api || coreState !== "online") return;
    void (async () => {
      try {
        setState((await api.credentialVaultStatus()).state);
      } catch (error) {
        void logCaughtDiagnostic("interface.credentials.vault_status_failed", "The credential vault state could not be read.", error, "integrations");
      }
    })();
  }, [api, coreState]);

  useEffect(() => { refresh(); }, [refresh]);

  return { state, available: state === "available", refresh };
}
