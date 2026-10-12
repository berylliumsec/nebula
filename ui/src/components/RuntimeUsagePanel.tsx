import { useEffect, useRef, useState } from "react";
import { Activity, Bot, Cloud, RefreshCw } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { RuntimeUsageBalance, RuntimeUsageItem, RuntimeUsageResponse, RuntimeUsageWindow } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";

const whole = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const decimal = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function figure(balance: RuntimeUsageBalance): string {
  if (balance.displayValue) return balance.displayValue;
  if (balance.remaining !== undefined) {
    if (balance.currency === "USD") return `$${decimal.format(balance.remaining)}`;
    if (balance.currency === "CNY") return `¥${decimal.format(balance.remaining)}`;
    return decimal.format(balance.remaining);
  }
  return balance.limit === undefined && balance.used !== undefined ? "No key cap" : "Balance unavailable";
}

function timeLabel(value?: string): string {
  if (!value) return "Not checked";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Not checked" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function Meter({ label, usedPercent }: { label: string; usedPercent: number }) {
  const used = Math.max(0, Math.min(100, usedPercent));
  return <div className="runtime-usage-meter" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={used} aria-valuetext={`${usedPercent}% used`}>
    <span className="runtime-usage-meter-track"><span style={{ width: `${used}%` }} /></span>
  </div>;
}

function WindowRow({ window }: { window: RuntimeUsageWindow }) {
  const remaining = Math.max(0, 100 - window.usedPercent);
  const duration = window.durationMinutes
    ? window.durationMinutes >= 60 && window.durationMinutes % 60 === 0
      ? `${window.durationMinutes / 60}h`
      : `${window.durationMinutes}m`
    : undefined;
  return <div className="runtime-usage-limit-row">
    <div><span>{window.label}{duration ? ` · ${duration}` : ""}</span><strong>{remaining}% left</strong></div>
    <Meter label={window.label} usedPercent={window.usedPercent} />
    {window.resetsAt && <small>Resets {timeLabel(window.resetsAt)}</small>}
  </div>;
}

function UsageCard({ item }: { item: RuntimeUsageItem }) {
  const balance = item.vendor.balances[0];
  const window = item.vendor.windows[0];
  const title = item.kind === "harness" ? "Agent harness" : "Model provider";
  const Icon = item.kind === "harness" ? Bot : Cloud;
  const usedPercent = balance?.limit && balance.used !== undefined && balance.limit > 0
    ? Math.round(balance.used / balance.limit * 100)
    : undefined;
  return <article className={`runtime-usage-card vendor-${item.vendor.state}`} aria-label={`${item.name} usage`}>
    <header>
      <span className="runtime-usage-icon"><Icon size={18} aria-hidden="true" /></span>
      <div><small>{title}</small><h3 title={item.name}>{item.name}</h3></div>
      {!item.enabled && <span className="runtime-usage-disabled">Disabled</span>}
    </header>
    <div className="runtime-usage-hero">
      <small>{balance?.label ?? (window ? "Included usage" : "Remaining balance")}</small>
      <div className="runtime-usage-figure">
        <strong>{balance ? figure(balance) : window ? `${Math.max(0, 100 - window.usedPercent)}%` : item.vendor.state === "error" ? "Could not refresh" : "Unavailable"}</strong>
        {(balance?.remaining !== undefined || window) && <span>{window && !balance ? "left" : "remaining"}</span>}
      </div>
      {item.vendor.ordinaryUsageAllowed === false && <p className="runtime-usage-attention">Included usage is currently unavailable.</p>}
      <p className="runtime-usage-detail">{item.vendor.detail}</p>
    </div>
    {(usedPercent !== undefined || item.vendor.windows.length > 0 || item.vendor.balances.length > 1) && <div className="runtime-usage-limits">
      {usedPercent !== undefined && <div className="runtime-usage-limit-row">
        <div><span>Key spending cap</span><strong>{Math.max(0, 100 - usedPercent)}% left</strong></div>
        <Meter label="Key spending cap" usedPercent={usedPercent} />
        <small>{balance?.used !== undefined ? `$${decimal.format(balance.used)} used` : ""}{balance?.limit !== undefined ? ` of $${decimal.format(balance.limit)}` : ""}</small>
      </div>}
      {item.vendor.windows.map((entry, index) => <WindowRow window={entry} key={`${entry.label}-${index}`} />)}
      {item.vendor.balances.slice(1).map((entry, index) => <div className="runtime-usage-extra-balance" key={`${entry.currency}-${index}`}><span>{entry.label} · {entry.currency ?? "credits"}</span><strong>{figure(entry)}</strong></div>)}
    </div>}
    <div className="runtime-usage-recorded">
      <span><strong>{whole.format(item.recorded.totalTokens)}</strong><small>tokens tracked in Nebula</small></span>
      <span><strong>{whole.format(item.recorded.turns)}</strong><small>usage records</small></span>
    </div>
    <details className="runtime-usage-details">
      <summary>Usage details</summary>
      <dl>
        <div><dt>Input</dt><dd>{whole.format(item.recorded.inputTokens)} tokens</dd></div>
        <div><dt>Cached input</dt><dd>{whole.format(item.recorded.cachedInputTokens)} tokens</dd></div>
        <div><dt>Output</dt><dd>{whole.format(item.recorded.outputTokens)} tokens</dd></div>
      </dl>
      <p>{item.recorded.scope}. Activity outside Nebula is excluded from these token counts.</p>
      {balance?.note && <p>{balance.note}</p>}
    </details>
    <footer><span>{item.vendor.source}</span><time dateTime={item.vendor.checkedAt}>{timeLabel(item.vendor.checkedAt)}</time></footer>
  </article>;
}

export function RuntimeUsagePanel({ api, active }: { api?: ApiClient; active: boolean }) {
  const [usage, setUsage] = useState<RuntimeUsageResponse>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const requestRef = useRef<AbortController | undefined>(undefined);

  useEffect(() => {
    if (!active || !api) return;
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    setError(undefined);
    void api.getRuntimeUsage(false, controller.signal).then((value) => {
      if (!controller.signal.aborted) setUsage(value);
    }).catch((loadError) => {
      if (controller.signal.aborted) return;
      void logCaughtDiagnostic("interface.runtime_usage.load_failed", "Provider and harness usage could not be loaded.", loadError, "runtime_usage");
      setError("Usage could not be loaded. Check the Core connection and retry.");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => requestRef.current?.abort();
  }, [active, api]);

  const refresh = async () => {
    if (!api || loading) return;
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    setError(undefined);
    try {
      const value = await api.getRuntimeUsage(true, controller.signal);
      if (!controller.signal.aborted) setUsage(value);
    } catch (refreshError) {
      if (controller.signal.aborted) return;
      void logCaughtDiagnostic("interface.runtime_usage.refresh_failed", "Provider and harness usage could not be refreshed.", refreshError, "runtime_usage");
      setError("Usage could not be refreshed. Saved profiles and earlier figures remain visible; retry when the connection recovers.");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };

  const providers = usage?.items.filter((item) => item.kind === "provider") ?? [];
  const harnesses = usage?.items.filter((item) => item.kind === "harness") ?? [];
  return <section className="settings-section runtime-usage-panel" id="runtime-usage" aria-label="Provider and harness usage">
    <div className="section-heading"><div><h2>Balances & activity</h2><p>Account figures come from each vendor; tokens come from Nebula.</p></div><button className="icon-button subtle runtime-usage-refresh" type="button" aria-label="Refresh provider and harness usage" title="Refresh usage" aria-busy={loading} disabled={!api || loading} onClick={() => void refresh()}><RefreshCw size={17} className={loading ? "spin" : undefined} aria-hidden="true" /></button></div>
    {error && <div className="runtime-usage-error" role="alert"><span>{error}</span><button className="button quiet" type="button" onClick={() => void refresh()}>Retry</button></div>}
    {loading && !usage && <div className="runtime-usage-loading" role="status"><Activity size={19} aria-hidden="true" /> Reading account usage…</div>}
    {usage && <>
      <div className="runtime-usage-overview" aria-label="Usage overview">
        <div><strong>{providers.length + harnesses.length}</strong><span>configured profiles</span></div>
        <div><strong>{usage.items.filter((item) => item.vendor.state === "available").length}</strong><span>vendor snapshots</span></div>
        <small>Updated {timeLabel(usage.generatedAt)}</small>
      </div>
      {usage.items.length ? <>
        {!!providers.length && <div className="runtime-usage-group"><h3>Model providers <span>{providers.length}</span></h3><div className="runtime-usage-grid">{providers.map((item) => <UsageCard item={item} key={item.profileId} />)}</div></div>}
        {!!harnesses.length && <div className="runtime-usage-group"><h3>Agent harnesses <span>{harnesses.length}</span></h3><div className="runtime-usage-grid">{harnesses.map((item) => <UsageCard item={item} key={item.profileId} />)}</div></div>}
      </> : <div className="runtime-usage-empty"><Activity size={22} aria-hidden="true" /><strong>No runtimes configured</strong><p>Add a provider or harness to see usage here.</p></div>}
    </>}
  </section>;
}
