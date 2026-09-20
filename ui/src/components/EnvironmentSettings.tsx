import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { ChevronRight, Command, FileText, RefreshCw, Server, TerminalSquare, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { SshEnvironment, SshEnvironmentApproval, SshEnvironmentDiscovery, SshEnvironmentHost } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { ModalSurface } from "./DialogSystem";
import { StandardEmptyState } from "./SurfacePrimitives";
import { announceSettingsSaved } from "./SettingsSaveFeedback";
import styles from "./EnvironmentSettings.module.css";

type Tone = "healthy" | "warning" | "critical" | "muted";

/** One-line reachability summary for a host row, derived from the last test. */
export function hostStatus(host: SshEnvironmentHost): { label: string; tone: Tone } {
  if (!host.inConfig) return { label: "Removed from ~/.ssh/config", tone: "warning" };
  if (host.resolveError) return { label: "Config error", tone: "warning" };
  const probe = host.environment?.lastProbe;
  if (!probe) return { label: host.environment?.enabled ? "Not tested" : "Not enabled", tone: "muted" };
  switch (probe.status) {
    case "reachable": return { label: probe.latencyMs !== undefined ? `Reachable · ${probe.latencyMs} ms` : "Reachable", tone: "healthy" };
    case "host_key_untrusted": return { label: "Host key not trusted", tone: "warning" };
    case "auth_failed": return { label: "Sign-in failed", tone: "critical" };
    case "unreachable": return { label: "Unreachable", tone: "critical" };
    default: return { label: "Test failed", tone: "critical" };
  }
}

function hostFacts(environment?: SshEnvironment): string[] {
  const probe = environment?.lastProbe;
  if (!probe || probe.status !== "reachable") return [];
  return [probe.osVersion || probe.system, probe.arch, probe.model].filter(Boolean);
}

function isMac(host: SshEnvironmentHost): boolean {
  return host.environment?.lastProbe?.system === "Darwin";
}

function connectionLabel(host: SshEnvironmentHost): string {
  const resolved = host.resolved;
  if (!resolved) return host.alias;
  const target = `${resolved.user ? `${resolved.user}@` : ""}${resolved.hostname}${resolved.port !== 22 ? `:${resolved.port}` : ""}`;
  return `${host.alias} · ${target}`;
}

function recoveryHint(host: SshEnvironmentHost): string | undefined {
  const probe = host.environment?.lastProbe;
  if (!host.inConfig) return "This Host entry is gone from ~/.ssh/config. Restore it, or forget these settings.";
  if (host.resolveError) return host.resolveError;
  switch (probe?.status) {
    case "host_key_untrusted": return `Connect once with ssh ${host.alias} on the Nebula host to confirm its host key, then test again.`;
    case "auth_failed": return "The key was refused. Check IdentityFile for this host, or load the key into ssh-agent on the Nebula host.";
    case "unreachable": return probe.detail || "No response. Check the host is powered on and on the network.";
    case "error": return probe.detail || undefined;
    default: return undefined;
  }
}

function relativeTime(iso: string, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return new Date(iso).toLocaleString();
}

// ``ssh -G`` reports keywords in lowercase; show them as written in ssh_config.
const OPTION_NAMES: Record<string, string> = {
  batchmode: "BatchMode",
  controlmaster: "ControlMaster",
  hostkeyalias: "HostKeyAlias",
  proxyjump: "ProxyJump",
  serveraliveinterval: "ServerAliveInterval",
  stricthostkeychecking: "StrictHostKeyChecking",
};

function failureMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function EnvironmentSettings() {
  const { api, previewMode } = useWorkspace();
  const [discovery, setDiscovery] = useState<SshEnvironmentDiscovery>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();
  const [detailsAlias, setDetailsAlias] = useState<string>();
  const [showOthers, setShowOthers] = useState(false);
  // Which hosts fold away is decided when the list loads, so enabling a host
  // never makes the rows below it jump.
  const [foldedAliases, setFoldedAliases] = useState<ReadonlySet<string>>(new Set());

  const reload = useCallback(async (signal?: AbortSignal) => {
    if (!api) return;
    setLoading(true);
    try {
      const next = await api.discoverSshEnvironments(signal);
      const configured = next.hosts.some((host) => host.environment);
      setFoldedAliases(new Set(configured ? next.hosts.filter((host) => !host.environment).map((host) => host.alias) : []));
      setDiscovery(next);
      setError(undefined);
    } catch (loadError) {
      if (signal?.aborted) return;
      void logCaughtDiagnostic("interface.environment_settings.caught_failure_01", "A handled interface operation failed.", loadError, "environment_settings");
      setError(failureMessage(loadError, "Could not read SSH hosts."));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, [reload]);

  const replaceEnvironment = useCallback((saved: SshEnvironment) => {
    setDiscovery((current) => current && {
      ...current,
      hosts: current.hosts.map((host) => host.alias === saved.alias ? { ...host, environment: saved } : host),
    });
  }, []);

  // The details dialog passes its own reporter so a failed test shows inside the
  // modal rather than in the section notice hidden behind it.
  const test = useCallback(async (host: SshEnvironmentHost, report: (message: string) => void = setError) => {
    if (!api) return;
    setBusy(host.alias);
    try {
      replaceEnvironment(await api.probeSshEnvironment(host.alias));
    } catch (probeError) {
      void logCaughtDiagnostic("interface.environment_settings.caught_failure_02", "A handled interface operation failed.", probeError, "environment_settings");
      report(failureMessage(probeError, "Connection test failed to run."));
    } finally {
      setBusy(undefined);
    }
  }, [api, replaceEnvironment]);

  const setEnabled = useCallback(async (host: SshEnvironmentHost, enabled: boolean) => {
    if (!api) return;
    setBusy(host.alias);
    try {
      let saved = await api.saveSshEnvironment(host.alias, { enabled }, host.environment?.revision);
      replaceEnvironment(saved);
      // A first test tells agents what the machine is; run it on first enable.
      if (enabled && !saved.lastProbe) {
        saved = await api.probeSshEnvironment(host.alias);
        replaceEnvironment(saved);
      }
      announceSettingsSaved(enabled ? `${saved.label} enabled` : `${saved.label} disabled`);
    } catch (saveError) {
      void logCaughtDiagnostic("interface.environment_settings.caught_failure_03", "A handled interface operation failed.", saveError, "environment_settings");
      setError(failureMessage(saveError, "Could not save the host."));
      void reload();
    } finally {
      setBusy(undefined);
    }
  }, [api, reload, replaceEnvironment]);

  const hosts = discovery?.hosts ?? [];
  const enabledCount = hosts.filter((host) => host.environment?.enabled).length;
  // Before anything is set up, the whole config is the list; afterwards the
  // untouched hosts fold away under one line.
  const primary = hosts.filter((host) => !foldedAliases.has(host.alias));
  const folded = hosts.filter((host) => foldedAliases.has(host.alias));
  const detailsHost = hosts.find((host) => host.alias === detailsAlias);

  return <section className="settings-section" id="ssh-environment-settings" data-guide="ssh-environment-settings">
    <div className="section-heading">
      <div><h2>Environments</h2><p>Machines Nebula can run commands on, besides the Core host</p></div>
      <button className="button secondary" type="button" disabled={loading || !api} onClick={() => void reload()}><RefreshCw className={loading ? "spin" : undefined} size={16} /> Reload</button>
    </div>
    {error && <DiagnosticErrorNotice error={error} fallback="The environment operation could not be completed." compact />}
    {discovery && <div className={styles.source}>
      <FileText size={16} aria-hidden="true" />
      <div>
        <code>{discovery.configPath}</code>
        <small>
          {discovery.configExists
            ? `Nebula host · ${hosts.filter((host) => host.inConfig).length} host${hosts.length === 1 ? "" : "s"} · ${enabledCount} enabled · read ${relativeTime(discovery.readAt)}`
            : "Not found on the Nebula host"}
        </small>
      </div>
      {(discovery.skippedPatterns.length > 0 || discovery.skippedMatchBlocks > 0) && <small className={styles.skipped} title="Wildcard Host patterns and Match blocks are not machines, so they are not listed.">
        Skipped: {[...discovery.skippedPatterns.map((pattern) => `Host ${pattern}`), ...(discovery.skippedMatchBlocks ? [`${discovery.skippedMatchBlocks} Match block${discovery.skippedMatchBlocks === 1 ? "" : "s"}`] : [])].join(", ")}
      </small>}
    </div>}
    {discovery && !discovery.sshAvailable && <p role="alert" className="integration-card-summary">The ssh client is not installed on the Nebula host, so hosts cannot be tested or used.</p>}
    {discovery?.errors.map((item) => <p key={item} role="alert" className="integration-card-summary">{item}</p>)}
    {discovery && !hosts.length && <StandardEmptyState compact icon={<Server size={23} />} title="No SSH hosts" explanation={`Add a Host entry to ${discovery.configPath} on the Nebula host, then Reload. Nebula reads that file and never changes it.`} />}
    {primary.length > 0 && <ul className={styles.list} aria-label="SSH hosts">
      {primary.map((host) => <HostRow key={host.alias} host={host} busy={busy === host.alias} disabled={previewMode || !api} onTest={test} onToggle={setEnabled} onOpen={setDetailsAlias} />)}
    </ul>}
    {folded.length > 0 && <div className={styles.more}>
      <button type="button" className={styles.moreToggle} aria-expanded={showOthers} onClick={() => setShowOthers((value) => !value)}>
        <ChevronRight size={14} aria-hidden="true" className={showOthers ? styles.open : undefined} />
        {folded.length} more host{folded.length === 1 ? "" : "s"} in ~/.ssh/config
        {!showOthers && <code>{folded.map((host) => host.alias).join(" · ")}</code>}
      </button>
      {showOthers && <ul className={styles.list} aria-label="Other SSH hosts">
        {folded.map((host) => <HostRow key={host.alias} host={host} busy={busy === host.alias} disabled={previewMode || !api} onTest={test} onToggle={setEnabled} onOpen={setDetailsAlias} />)}
      </ul>}
    </div>}
    {detailsHost && api && <HostDetailsDialog
      api={api}
      host={detailsHost}
      busy={busy === detailsHost.alias}
      onTest={test}
      onClose={() => setDetailsAlias(undefined)}
      onSaved={(saved) => { replaceEnvironment(saved); announceSettingsSaved(`${saved.label} saved`); }}
      onForgotten={() => { setDetailsAlias(undefined); void reload(); }}
    />}
  </section>;
}

interface HostRowProps {
  host: SshEnvironmentHost;
  busy: boolean;
  disabled: boolean;
  onTest: (host: SshEnvironmentHost) => Promise<void>;
  onToggle: (host: SshEnvironmentHost, enabled: boolean) => Promise<void>;
  onOpen: (alias: string) => void;
}

function HostRow({ host, busy, disabled, onTest, onToggle, onOpen }: HostRowProps) {
  const environment = host.environment;
  const enabled = Boolean(environment?.enabled);
  const label = environment?.label ?? host.alias;
  const status = hostStatus(host);
  const hint = recoveryHint(host);
  const Glyph = isMac(host) ? Command : TerminalSquare;
  return <li className={`${styles.row} ${enabled ? "" : styles.inactive}`}>
    <span className={styles.glyph} aria-hidden="true"><Glyph size={15} /></span>
    <button type="button" className={styles.name} onClick={() => onOpen(host.alias)} aria-label={`${label} details`}>
      <strong>{label}</strong>
      <code>{connectionLabel(host)}</code>
      {hint && <small className={styles.hint}>{hint}</small>}
    </button>
    <span className={styles.facts}>{hostFacts(environment).map((fact) => <span key={fact} className={styles.chip}>{fact}</span>)}</span>
    <span className={`${styles.status} ${styles[status.tone]}`} role="status"><span className={styles.dot} aria-hidden="true" />{status.label}</span>
    <button className={`button quiet ${styles.test}`} type="button" disabled={busy || disabled || !host.inConfig} onClick={() => void onTest(host)} aria-label={`Test connection to ${label}`}>
      <RefreshCw className={busy ? "spin" : undefined} size={14} /><span className={styles.testLabel}>{busy ? "Testing" : "Test"}</span>
    </button>
    <label className={`${styles.switch} ${styles.toggle}`} title={enabled ? `Disable ${label}` : `Enable ${label}`}>
      <input type="checkbox" role="switch" checked={enabled} disabled={busy || disabled || (!enabled && !host.inConfig)} aria-label={`Use ${label} from Nebula`} onChange={(event) => void onToggle(host, event.target.checked)} />
      <span aria-hidden="true" />
    </label>
  </li>;
}

interface HostDetailsDialogProps {
  api: ApiClient;
  host: SshEnvironmentHost;
  busy: boolean;
  onTest: (host: SshEnvironmentHost, onError: (message: string) => void) => Promise<void>;
  onClose: () => void;
  onSaved: (environment: SshEnvironment) => void;
  onForgotten: () => void;
}

function HostDetailsDialog({ api, host, busy, onTest, onClose, onSaved, onForgotten }: HostDetailsDialogProps) {
  const environment = host.environment;
  const [displayName, setDisplayName] = useState(environment?.displayName ?? "");
  const [notes, setNotes] = useState(environment?.notes || host.comment);
  const [workingDirectory, setWorkingDirectory] = useState(environment?.workingDirectory ?? "");
  const [enabled, setEnabled] = useState(Boolean(environment?.enabled));
  const [approval, setApproval] = useState<SshEnvironmentApproval>(environment?.commandApproval ?? "ask");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const probe = environment?.lastProbe;
  const label = displayName.trim() || host.alias;

  const facts = useMemo(() => {
    const resolved = host.resolved;
    const rows: Array<[string, string]> = [];
    if (resolved) {
      rows.push(["HostName", resolved.hostname], ["User", resolved.user || "(ssh default)"]);
      if (resolved.port !== 22) rows.push(["Port", String(resolved.port)]);
      rows.push(["Identity", resolved.identityFiles.length ? resolved.identityFiles.map((file) => file.split("/").pop()).join(", ") : "ssh defaults"]);
      for (const [key, value] of Object.entries(resolved.options)) rows.push([OPTION_NAMES[key] ?? key, value]);
    }
    if (host.aliases.length > 1) rows.push(["Also known as", host.aliases.slice(1).join(", ")]);
    if (host.comment) rows.push(["Comment", host.comment]);
    return rows;
  }, [host]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      onSaved(await api.saveSshEnvironment(host.alias, { displayName, notes, workingDirectory, enabled, commandApproval: approval }, environment?.revision));
      onClose();
    } catch (saveError) {
      void logCaughtDiagnostic("interface.environment_settings.caught_failure_04", "A handled interface operation failed.", saveError, "environment_settings");
      setError(failureMessage(saveError, "Could not save the host."));
    } finally {
      setSaving(false);
    }
  }

  async function forget() {
    setSaving(true);
    try {
      await api.forgetSshEnvironment(host.alias);
      onForgotten();
    } catch (forgetError) {
      void logCaughtDiagnostic("interface.environment_settings.caught_failure_05", "A handled interface operation failed.", forgetError, "environment_settings");
      setError(failureMessage(forgetError, "Could not forget the host."));
      setSaving(false);
    }
  }

  const checks: Array<{ ok: boolean; label: string; value: string }> = probe?.status === "reachable" ? [
    { ok: true, label: "SSH", value: probe.latencyMs !== undefined ? `Connected in ${probe.latencyMs} ms` : "Connected" },
    { ok: true, label: "System", value: [probe.osVersion || probe.system, probe.arch, probe.model].filter(Boolean).join(" · ") },
    { ok: probe.tools.length > 0, label: "Tools", value: probe.tools.length ? probe.tools.join(" · ") : "None of the common tools found" },
    { ok: probe.passwordlessSudo === true, label: "sudo", value: probe.passwordlessSudo ? "Works without a password" : "Needs a password; agents will avoid it" },
  ] : probe ? [{ ok: false, label: "SSH", value: hostStatus(host).label + (probe.detail ? ` — ${probe.detail}` : "") }] : [];

  const close = () => { if (!saving) onClose(); };

  return <ModalSurface as="form" className={`provider-dialog resource-dialog ${styles.dialog}`} labelledBy="ssh-environment-dialog-title" onClose={close} onSubmit={(event) => void submit(event)}>
    <header>
      <div><small>SSH environment</small><h2 id="ssh-environment-dialog-title">{label}</h2></div>
      <button className="icon-button subtle" type="button" aria-label="Close host details" disabled={saving} onClick={close}><X size={17} /></button>
    </header>
    <div className={styles.columns}>
      <div className={styles.column}>
        <section className={styles.card} aria-labelledby="ssh-connection-heading">
          <h3 id="ssh-connection-heading">Connection</h3>
          <p className={styles.muted}>{host.inConfig ? `From ${host.source ?? "~/.ssh/config"}${host.line ? `, line ${host.line}` : ""}. Edit that file to change these.` : "This host is no longer in ~/.ssh/config."}</p>
          {facts.length > 0 && <dl className={styles.factsList}>{facts.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>}
        </section>
        <section className={styles.card} aria-labelledby="ssh-nebula-heading">
          <h3 id="ssh-nebula-heading">Nebula settings</h3>
          <label>Display name<input value={displayName} placeholder={host.alias} maxLength={200} onChange={(event) => setDisplayName(event.target.value)} /></label>
          <label>Notes for agents<textarea rows={3} maxLength={4000} value={notes} placeholder="What this machine is for, and anything an agent should check first." onChange={(event) => setNotes(event.target.value)} /><small className={styles.muted}>Given to the model whenever this host is available in a chat.</small></label>
          <label>Working directory<input value={workingDirectory} placeholder="Home directory" maxLength={1024} className={styles.mono} onChange={(event) => setWorkingDirectory(event.target.value)} /></label>
        </section>
      </div>
      <div className={styles.column}>
        <section className={styles.card} aria-labelledby="ssh-access-heading">
          <h3 id="ssh-access-heading">Access</h3>
          <label className={styles.accessRow}>
            <span><strong>Enabled</strong><small>Available in chats</small></span>
            <span className={styles.switch}><input type="checkbox" role="switch" checked={enabled} disabled={!host.inConfig && !enabled} onChange={(event) => setEnabled(event.target.checked)} /><span aria-hidden="true" /></span>
          </label>
          <label className={styles.accessRow}>
            <span><strong>Run commands</strong><small>Shell over SSH, shown in chat</small></span>
            <select value={approval} onChange={(event) => setApproval(event.target.value as SshEnvironmentApproval)} aria-label="Command approval"><option value="ask">Ask each time</option><option value="allow">Allow</option></select>
          </label>
        </section>
        <section className={styles.card} aria-labelledby="ssh-test-heading">
          <h3 id="ssh-test-heading">Last connection test</h3>
          {probe ? <p className={styles.muted}>{new Date(probe.checkedAt).toLocaleString()}</p> : <p className={styles.muted}>Not tested yet.</p>}
          {checks.length > 0 && <ul className={styles.checks}>{checks.map((check) => <li key={check.label}><span className={check.ok ? styles.healthy : styles.warning} aria-label={check.ok ? "passed" : "attention"}>{check.ok ? "✓" : "!"}</span><strong>{check.label}</strong><span>{check.value}</span></li>)}</ul>}
          <button className="button secondary" type="button" disabled={busy || !host.inConfig} onClick={() => { setError(undefined); void onTest(host, setError); }}><RefreshCw className={busy ? "spin" : undefined} size={14} /> {busy ? "Testing" : probe ? "Test again" : "Test connection"}</button>
        </section>
      </div>
    </div>
    {error && <DiagnosticErrorNotice error={error} fallback="The environment operation could not be completed." compact />}
    <footer>
      {environment && <button className="button quiet" type="button" disabled={saving} onClick={() => void forget()} title="Remove Nebula's settings for this host. ~/.ssh/config is not changed.">Forget settings</button>}
      <button className="button secondary" type="button" onClick={close} disabled={saving}>Cancel</button>
      <button className="button primary" type="submit" disabled={saving || (!host.inConfig && !environment)}>{saving ? "Saving…" : "Save"}</button>
    </footer>
  </ModalSurface>;
}
