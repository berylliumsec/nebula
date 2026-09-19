import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Sparkles } from "lucide-react";
import type { TypeSafeIntegration } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { vaultUnavailableNote } from "../hooks/useCredentialVault";
import { announceSettingsSaved } from "./SettingsSaveFeedback";
import { useConfirmation } from "./DialogSystem";

/** Other mounted settings (the project opt-in) follow key changes without a reload. */
export const TYPESAFE_CHANGED_EVENT = "nebula:typesafe-integration-changed";

function publish(status: TypeSafeIntegration) {
  window.dispatchEvent(new CustomEvent<TypeSafeIntegration>(TYPESAFE_CHANGED_EVENT, { detail: status }));
}

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

export function testedAgo(testedAt: string, now = Date.now()): string {
  const seconds = Math.round((Date.parse(testedAt) - now) / 1000);
  if (Math.abs(seconds) < 60) return relative.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return relative.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return relative.format(hours, "hour");
  return relative.format(Math.round(hours / 24), "day");
}

type Health = { label: string; tone: "muted" | "good" | "bad" };

export function integrationHealth(status: TypeSafeIntegration | undefined): Health {
  if (!status?.source) return { label: "Not configured", tone: "muted" };
  if (!status.available) return { label: "Key unavailable", tone: "bad" };
  if (!status.lastTest) return { label: "Not tested", tone: "muted" };
  return status.lastTest.ok ? { label: "Working", tone: "good" } : { label: "Not working", tone: "bad" };
}

const sourceLabel = {
  vault: "Saved in OS keychain",
  session: "Saved for this Nebula session only",
  environment: "TYPESAFE_API_KEY from Core's environment",
} as const;

export function TypeSafeIntegrationSettings() {
  const confirm = useConfirmation();
  const { api, coreState, previewMode } = useWorkspace();
  const [status, setStatus] = useState<TypeSafeIntegration>();
  const [secret, setSecret] = useState("");
  const [sessionOnly, setSessionOnly] = useState(false);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<"save" | "test" | "remove">();
  const [error, setError] = useState<unknown>();

  const load = useCallback(async () => {
    if (!api || coreState !== "online") return;
    try {
      setStatus(await api.getTypeSafeIntegration());
      setError(undefined);
    } catch (loadError) {
      void logCaughtDiagnostic("interface.typesafe.load_failed", "TypeSafe integration status could not be loaded.", loadError, "integrations");
      setError(loadError);
    }
  }, [api, coreState]);

  useEffect(() => { void load(); }, [load]);

  const run = async (kind: "save" | "test" | "remove", action: () => Promise<TypeSafeIntegration>, saved?: string) => {
    setBusy(kind);
    setError(undefined);
    try {
      const next = await action();
      setStatus(next);
      publish(next);
      if (saved) announceSettingsSaved(saved);
      return next;
    } catch (actionError) {
      void logCaughtDiagnostic(`interface.typesafe.${kind}_failed`, "The TypeSafe key operation failed.", actionError, "integrations");
      setError(actionError);
      return undefined;
    } finally {
      setBusy(undefined);
    }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !secret.trim()) return;
    const persistence = sessionOnly || !status?.vaultAvailable ? "session" : "vault";
    const next = await run("save", () => api.saveTypeSafeKey(secret.trim(), persistence), "TypeSafe key saved");
    if (next) { setSecret(""); setEditing(false); }
  };

  const testKey = () => {
    if (api) void run("test", () => api.testTypeSafeKey());
  };

  const remove = async () => {
    if (!api) return;
    const approved = await confirm({
      title: "Remove the TypeSafe key?",
      message: <>Projects that use tool suggestions keep working, without suggestions.{status?.projectsUsing ? ` ${status.projectsUsing} project${status.projectsUsing === 1 ? " uses" : "s use"} it.` : ""}</>,
      confirmLabel: "Remove key",
      tone: "danger",
    });
    if (approved) await run("remove", () => api.removeTypeSafeKey(), "TypeSafe key removed");
  };

  const health = integrationHealth(status);
  const saved = status?.source === "vault" || status?.source === "session";
  const showForm = !saved || editing;
  const disabled = previewMode || !api || coreState !== "online" || Boolean(busy);
  const test = status?.lastTest;

  return <section className="settings-section" id="typesafe-integration-settings">
    <div className="section-heading"><div><h2>Integrations</h2><p>Services Nebula can call on your behalf. Keys are held by Core and never shown again.</p></div><Sparkles size={20} /></div>
    {Boolean(error) && <DiagnosticErrorNotice error={error} fallback="The TypeSafe key operation failed." compact />}
    <div className="panel typesafe-panel">
      <header className="panel-header compact typesafe-header">
        <div><h3>Tool suggestions · TypeSafe Jev</h3><p>Jev predicts which MCP tools a turn needs so the model loads only those. Each project opts in separately.</p></div>
        <span className={`integration-status ${health.tone}`} role="status"><span aria-hidden="true" />{health.label}</span>
      </header>
      {status?.source && <dl className="integration-facts">
        <div><dt>Key</dt><dd>{sourceLabel[status.source]}</dd></div>
        {test && <div><dt>Last test</dt><dd>{test.ok ? [testedAgo(test.testedAt), test.latencyMs !== undefined ? `${test.latencyMs} ms` : undefined, test.model].filter(Boolean).join(" · ") : `${testedAgo(test.testedAt)} · ${test.error ?? "failed"}`}</dd></div>}
        <div><dt>In use by</dt><dd>{status.projectsUsing === 1 ? "1 project" : `${status.projectsUsing} projects`}</dd></div>
      </dl>}
      {showForm ? <form className="typesafe-form" onSubmit={(event) => void save(event)}>
        <label>TypeSafe API key<input type="password" autoComplete="new-password" value={secret} placeholder="API key" disabled={disabled} onChange={(event) => setSecret(event.target.value)} /></label>
        {status?.vaultAvailable === false
          ? <p className="provider-dialog-note">{vaultUnavailableNote(status.vaultState, "key")}</p>
          : <label className="provider-consent"><input type="checkbox" checked={sessionOnly} disabled={disabled} onChange={(event) => setSessionOnly(event.target.checked)} /><span><strong>Use for this Nebula session only</strong><small>When off, Core saves the key in the operating-system credential vault. It is never returned or stored in the database.</small></span></label>}
        <footer>
          <span>{status?.source === "environment" ? "A saved key replaces the environment key." : "Without a saved key, Core uses TYPESAFE_API_KEY from its environment."}</span>
          <span className="typesafe-actions">
            {editing && <button className="button secondary" type="button" disabled={Boolean(busy)} onClick={() => { setEditing(false); setSecret(""); }}>Cancel</button>}
            {status?.source === "environment" && !editing && <button className="button secondary" type="button" disabled={disabled} onClick={testKey}>{busy === "test" ? "Testing…" : "Test"}</button>}
            <button className="button primary" type="submit" disabled={disabled || !secret.trim()}>{busy === "save" ? "Saving…" : "Save and test"}</button>
          </span>
        </footer>
      </form> : <footer className="typesafe-footer">
        <span>The test sends a fixed sample, never project data.</span>
        <span className="typesafe-actions">
          <button className="button secondary" type="button" disabled={disabled} onClick={testKey}>{busy === "test" ? "Testing…" : "Test"}</button>
          <button className="button secondary" type="button" disabled={disabled} onClick={() => setEditing(true)}>Replace key</button>
          <button className="button danger-quiet" type="button" disabled={disabled} onClick={() => void remove()}>{busy === "remove" ? "Removing…" : "Remove"}</button>
        </span>
      </footer>}
    </div>
  </section>;
}
