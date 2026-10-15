import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import type { ApiClient } from "../api/client";
import { logCaughtDiagnostic } from "../diagnostics";
import styles from "./OpenRouterCreditsPanel.module.css";

export interface OpenRouterCreditsSnapshot {
  provider_id: string;
  status: "observed" | "unavailable";
  checked_at: string;
  observed_at?: string | null;
  total_credits_usd?: number | null;
  total_usage_usd?: number | null;
  remaining_usd?: number | null;
  reason_code?: string | null;
}

function dollars(value: number): string {
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(value);
}

function timeLabel(value?: string | null): string {
  if (!value) return "Not observed";
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? "Not observed" : time.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function unavailableMessage(reason?: string | null): string {
  if (reason === "management_credential_missing") return "Add an OpenRouter management key to check account credits.";
  if (reason === "management_credential_unavailable") return "The management key is unavailable to Core. Check its vault or service credential.";
  if (reason === "openrouter_request_failed" || reason === "upstream_unavailable") return "OpenRouter did not return account credits. Retry the check.";
  return "Account credits could not be checked. Retry or check the management key.";
}

/** Account credits use a separate management key, never the inference key cap. */
export function OpenRouterCreditsPanel({ api, providerId, providerName, onConfigure, disabled = false }: {
  api?: ApiClient;
  providerId: string;
  providerName: string;
  onConfigure(): void;
  disabled?: boolean;
}) {
  const [snapshot, setSnapshot] = useState<OpenRouterCreditsSnapshot>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<AbortController | undefined>(undefined);
  const current = snapshot?.provider_id === providerId ? snapshot : undefined;

  const refresh = useCallback(async () => {
    request.current?.abort();
    if (!api || disabled) return;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError("");
    try {
      const next = await api.request<OpenRouterCreditsSnapshot>(
        `providers/${encodeURIComponent(providerId)}/openrouter/credits/refresh`,
        { method: "POST", signal: controller.signal },
      );
      if (!controller.signal.aborted) {
        if (next.provider_id !== providerId) throw new Error("Core returned credits for another provider.");
        setSnapshot(next);
      }
    } catch (caught) {
      if (controller.signal.aborted) return;
      void logCaughtDiagnostic("interface.openrouter_credits.refresh_failed", "OpenRouter account credits could not be refreshed.", caught, "provider_credits");
      setError(caught instanceof Error ? caught.message : "Could not check account credits.");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
      if (request.current === controller) request.current = undefined;
    }
  }, [api, providerId, disabled]);

  useEffect(() => {
    setSnapshot(undefined);
    setError("");
    void refresh();
    return () => request.current?.abort();
  }, [refresh]);

  return <section className={`runtime-usage-card ${styles.root}`} aria-label={`${providerName} account credits`}>
    <header>
      <div><small>OpenRouter account</small><h3>{providerName} credits</h3></div>
      <button className="icon-button subtle" type="button" aria-label={`Refresh ${providerName} account credits`} title="Refresh account credits" disabled={!api || disabled || loading} onClick={() => void refresh()}><RefreshCw size={17} aria-hidden="true" /></button>
    </header>
    {(!api || disabled) && <p>Account credit checks are unavailable in this view.</p>}
    {loading && !current && <p role="status">Checking account credits…</p>}
    {error && <p role="alert">Credit check failed. {error} <button className="button quiet" type="button" disabled={disabled} onClick={() => void refresh()}>Retry</button></p>}
    {current?.status === "observed" && current.remaining_usd !== null && current.remaining_usd !== undefined && <>
      <p><strong>{dollars(current.remaining_usd)} remaining</strong></p>
      {current.remaining_usd <= 20 && <p role="status">At or below the $20 account balance threshold.</p>}
      <small>Observed {timeLabel(current.observed_at ?? current.checked_at)}. This is account credit balance, separate from the inference key spending limit.</small>
      <details><summary>Credit details</summary><p>Total credits {current.total_credits_usd === null || current.total_credits_usd === undefined ? "unavailable" : dollars(current.total_credits_usd)} · total usage {current.total_usage_usd === null || current.total_usage_usd === undefined ? "unavailable" : dollars(current.total_usage_usd)}</p></details>
    </>}
    {current?.status === "observed" && (current.remaining_usd === null || current.remaining_usd === undefined) && <p role="status">OpenRouter did not provide a remaining balance. Retry the check.</p>}
    {current?.status === "unavailable" && <><p>{unavailableMessage(current.reason_code)}</p><small>Checked {timeLabel(current.checked_at)}. No balance is inferred from the inference key.</small><button className="button quiet" type="button" onClick={onConfigure}>Configure management key</button></>}
    {loading && current && <small role="status">Refreshing account credits…</small>}
  </section>;
}
