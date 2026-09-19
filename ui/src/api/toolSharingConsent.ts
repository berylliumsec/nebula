import type { ApiClient } from "./client";
import type { HarnessProfile, ProviderHealth } from "./types";
import type { ConfirmationOptions } from "../components/DialogSystem";
import { logCaughtDiagnostic } from "../diagnostics";

/** A chat or mission runtime that can receive bounded tool inputs and results. */
export type ToolSharingRuntime =
  | { kind: "provider"; profile: ProviderHealth }
  | { kind: "harness"; profile: HarnessProfile };

/** True when the operator already granted standing consent for this runtime. */
export function sharesToolResultsAlways(runtime: ToolSharingRuntime | undefined): boolean {
  return runtime?.profile.autoShareToolResults === true;
}

/**
 * Build the "always allow" checkbox for a tool-result confirmation. Consent is
 * stored on the runtime profile, so Settings stays the single place to revoke it.
 */
export function rememberToolSharing(
  api: ApiClient,
  runtime: ToolSharingRuntime,
  onSaved: (saved: ToolSharingRuntime) => void,
): ConfirmationOptions["remember"] {
  return {
    label: `Always allow for ${runtime.profile.name}`,
    hint: "Skips this prompt for every later turn until you turn it off in Settings.",
    onConfirm: (remember) => {
      if (!remember) return;
      void (async () => {
        try {
          onSaved(runtime.kind === "harness"
            ? { kind: "harness", profile: await api.setHarnessToolResultSharing(runtime.profile, true) }
            : { kind: "provider", profile: await api.setProviderToolResultSharing(runtime.profile, true) });
        } catch (error) {
          // The turn already carries this operator's consent; only the standing
          // preference is lost, so the next turn asks again instead of failing.
          void logCaughtDiagnostic(
            "interface.tool_sharing_consent.caught_failure_01",
            "Standing tool-result sharing consent could not be saved.",
            error,
            "tool_sharing_consent",
          );
        }
      })();
    },
  };
}
