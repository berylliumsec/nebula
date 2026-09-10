import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Save, ShieldCheck, TerminalSquare } from "lucide-react";
import type { AutomationProjectPolicy, EngagementScopePolicy, VpnProfile } from "../api/types";
import { ApiError } from "../api/client";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { announceSettingsSaved } from "./SettingsSaveFeedback";
import { InlineValidationNotice } from "./InlineValidationNotice";
import { useConfirmation } from "./DialogSystem";

function lines(value: string): string[] {
  return [...new Set(value.split(/[\n,]+/).map((item) => item.trim()).filter(Boolean))];
}

export function parseAllowedDomains(value: string): string[] | undefined {
  const normalized: string[] = [];
  for (const entry of lines(value)) {
    let domain = entry;
    if (entry.includes("://")) {
      try {
        const url = new URL(entry);
        if ((url.protocol !== "http:" && url.protocol !== "https:")
          || url.username || url.password || url.port
          || (url.pathname !== "/" && url.pathname !== "")
          || url.search || url.hash) return undefined;
        domain = url.hostname;
      } catch {
        // diagnostic-expected: malformed operator input is returned as inline validation.
        return undefined;
      }
    }
    normalized.push(domain.replace(/\.$/, "").toLocaleLowerCase());
  }
  return [...new Set(normalized)];
}

export function parseAllowedPorts(value: string): number[] | undefined {
  const ports = new Set<number>();
  for (const item of lines(value)) {
    const match = item.match(/^(\d+)\s*-\s*(\d+)$/);
    const start = Number(match?.[1] ?? item);
    const end = Number(match?.[2] ?? item);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > 65_535 || start > end) return undefined;
    for (let port = start; port <= end; port += 1) ports.add(port);
  }
  return [...ports].sort((left, right) => left - right);
}

function formatAllowedPorts(values: number[]): string {
  const ports = [...new Set(values)].sort((left, right) => left - right);
  const entries: string[] = [];
  for (let index = 0; index < ports.length;) {
    const start = ports[index];
    let end = start;
    while (index + 1 < ports.length && ports[index + 1] === end + 1) end = ports[++index];
    entries.push(end - start >= 2 ? `${start}-${end}` : end === start ? `${start}` : `${start}, ${end}`);
    index += 1;
  }
  return entries.join(", ");
}

function inputDate(value?: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  return new Date(date.valueOf() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function wireDate(value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? undefined : date.toISOString();
}

export function EngagementPolicySettings() {
  const confirm = useConfirmation();
  const { api, coreState, engagement, previewMode } = useWorkspace();
  const [scope, setScope] = useState<EngagementScopePolicy>();
  const [policy, setPolicy] = useState<AutomationProjectPolicy>();
  const [allowedCidrs, setAllowedCidrs] = useState("");
  const [allowedDomains, setAllowedDomains] = useState("");
  const [allowedUrls, setAllowedUrls] = useState("");
  const [allowedPorts, setAllowedPorts] = useState("");
  const [allowAllTargets, setAllowAllTargets] = useState(false);
  const [notBefore, setNotBefore] = useState("");
  const [notAfter, setNotAfter] = useState("");
  const [prohibitedActions, setProhibitedActions] = useState("");
  const [localOnly, setLocalOnly] = useState(true);
  const [maxConcurrency, setMaxConcurrency] = useState(1);
  const [approvalPolicy, setApprovalPolicy] = useState<AutomationProjectPolicy["approvalPolicy"]>("on_boundary");
  const [executionMode, setExecutionMode] = useState<"docker" | "host">("docker");
  const [hostAcknowledged, setHostAcknowledged] = useState(false);
  const [networkEnabled, setNetworkEnabled] = useState(false);
  const [vpnProfileId, setVpnProfileId] = useState("");
  const [vpnProfiles, setVpnProfiles] = useState<VpnProfile[]>([]);
  const [maxTimeoutMs, setMaxTimeoutMs] = useState(300_000);
  const [saving, setSaving] = useState<"scope" | "runtime">();
  const [error, setError] = useState<unknown>();
  const [validationError, setValidationError] = useState<string>();
  const loadRevision = useRef(0);
  const policyReady = Boolean(engagement && scope?.engagementId === engagement.id && policy?.engagementId === engagement.id);

  const applyScope = (next: EngagementScopePolicy) => {
    setScope(next);
    setAllowedCidrs(next.allowedCidrs.join("\n"));
    setAllowedDomains(next.allowedDomains.join("\n"));
    setAllowedUrls(next.allowedUrls.join("\n"));
    setAllowedPorts(formatAllowedPorts(next.allowedPorts));
    setAllowAllTargets(next.allowAllTargets);
    setNotBefore(inputDate(next.notBefore));
    setNotAfter(inputDate(next.notAfter));
    setProhibitedActions(next.prohibitedActions.join("\n"));
    setLocalOnly(next.localOnly);
    setMaxConcurrency(next.maxConcurrency);
  };

  const applyPolicy = (next: AutomationProjectPolicy) => {
    setPolicy(next);
    setExecutionMode(next.executionMode ?? "docker");
    setHostAcknowledged(next.hostAccessAcknowledged ?? false);
    setApprovalPolicy(next.approvalPolicy);
    setNetworkEnabled(next.networkEnabled);
    setVpnProfileId(next.vpnProfileId ?? "");
    setMaxTimeoutMs(next.maxTimeoutMs);
  };

  const load = useCallback(async () => {
    if (!api || coreState !== "online" || !engagement) return;
    const revision = ++loadRevision.current;
    setPolicy(undefined);
    setScope(undefined);
    setSaving(undefined);
    setError(undefined);
    try {
      const [nextScope, nextPolicy, nextVpnProfiles] = await Promise.all([
        api.getEngagementScope(engagement.id),
        api.getAutomationPolicy(engagement.id),
        api.listVpnProfiles().catch((vpnError) => {
          if (vpnError instanceof ApiError && (vpnError.status === 404 || vpnError.status === 501)) return [];
          void logCaughtDiagnostic("interface.engagement_policy.vpn_profiles_unavailable", "VPN profiles are unavailable in this Core build.", vpnError, "engagement_policy");
          return Promise.reject(vpnError);
        }),
      ]);
      if (revision !== loadRevision.current) return;
      applyScope(nextScope);
      applyPolicy(nextPolicy);
      setVpnProfiles(nextVpnProfiles);
    } catch (loadError) {
      if (revision !== loadRevision.current) return;
      void logCaughtDiagnostic("interface.execution_policy.caught_failure_01", "A handled interface operation failed.", loadError, "execution_policy");
      setError(loadError instanceof Error ? loadError.message : "Could not load project execution policy.");
    }
  }, [api, coreState, engagement?.id]);

  useEffect(() => { void load(); return () => { loadRevision.current += 1; }; }, [load]);

  const saveScope = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !engagement || !scope || !policyReady || saving || previewMode) return;
    const revision = loadRevision.current;
    const domains = parseAllowedDomains(allowedDomains);
    if (!domains) {
      setValidationError("Allowed domains accept a hostname or a root HTTP(S) URL such as www.example.com or https://www.example.com. Put URLs with paths or ports in URL-only scope.");
      return;
    }
    const ports = parseAllowedPorts(allowedPorts);
    if (!ports) {
      setValidationError("Allowed ports must be numbers or ascending ranges from 0 through 65535, such as 80, 443, or 0-400.");
      return;
    }
    const start = wireDate(notBefore);
    const end = wireDate(notAfter);
    if (start && end && start >= end) {
      setValidationError("The scope end time must be after its start time. Change one of the dates and save again.");
      return;
    }
    if (!Number.isInteger(maxConcurrency) || maxConcurrency < 1 || maxConcurrency > 256) {
      setValidationError("Maximum concurrency must be a whole number from 1 through 256.");
      return;
    }
    if (allowAllTargets && !scope.allowAllTargets) {
      const approved = await confirm({
        title: "Allow every network target and port?",
        message: <>This removes destination and port boundaries for Browser actions and project-scoped networking. Time windows, prohibited actions, privacy controls, and high-risk approval requirements still apply. Existing allowlist entries are retained for when you turn this mode off.</>,
        confirmLabel: "Allow all targets",
        tone: "danger",
      });
      if (!approved || revision !== loadRevision.current) return;
    }
    setSaving("scope"); setError(undefined); setValidationError(undefined);
    try {
      const next = await api.updateEngagementScope(engagement.id, {
        allowedCidrs: lines(allowedCidrs),
        allowedDomains: domains,
        allowedUrls: lines(allowedUrls),
        allowedPorts: ports,
        allowAllTargets,
        notBefore: start,
        notAfter: end,
        prohibitedActions: lines(prohibitedActions),
        localOnly,
        maxConcurrency,
        grants: scope.grants,
        expectedRevision: scope.revision,
      });
      if (revision !== loadRevision.current) return;
      applyScope(next);
      announceSettingsSaved("Network scope updated for new sessions.");
    } catch (saveError) {
      if (revision !== loadRevision.current) return;
      void logCaughtDiagnostic("interface.engagement_policy.scope_save_failed", "Project scope could not be saved.", saveError, "engagement_policy");
      setError(saveError);
    } finally { if (revision === loadRevision.current) setSaving(undefined); }
  };

  const saveRuntime = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !engagement || !policy || !policyReady || saving || previewMode) return;
    const revision = loadRevision.current;
    if (!Number.isInteger(maxTimeoutMs) || maxTimeoutMs < 1_000 || maxTimeoutMs > 86_400_000) {
      setValidationError("Maximum command timeout must be a whole number from 1000 through 86400000 milliseconds.");
      return;
    }
    if (executionMode === "host" && !hostAcknowledged) { setValidationError("Acknowledge host access before enabling Host mode."); return; }
    setSaving("runtime"); setError(undefined); setValidationError(undefined);
    try {
      const next = await api.updateAutomationPolicy(engagement.id, {
        executionMode,
        hostAccessAcknowledged: hostAcknowledged,
        approvalPolicy,
        networkEnabled,
        runnerProfileId: policy.runnerProfileId,
        vpnProfileId: vpnProfileId || undefined,
        maxTimeoutMs,
        expectedRevision: policy.revision,
      });
      if (revision !== loadRevision.current) return;
      applyPolicy(next);
      announceSettingsSaved("Runtime policy updated for new sessions.");
    } catch (saveError) {
      if (revision !== loadRevision.current) return;
      void logCaughtDiagnostic("interface.engagement_policy.runtime_save_failed", "Project command-runtime policy could not be saved.", saveError, "engagement_policy");
      setError(saveError);
    } finally { if (revision === loadRevision.current) setSaving(undefined); }
  };

  return <section className="settings-section" id="engagement-policy-settings">
    <div className="section-heading"><div><h2>Project execution policy</h2><p>Freeze the scope, approval behavior, and whole-project network boundary used by new agent sessions.</p></div><ShieldCheck size={20} /></div>
    {Boolean(error) && <DiagnosticErrorNotice error={error} fallback="The project policy could not be updated." compact />}
    {!policyReady && (error ? <button className="button secondary" type="button" onClick={() => void load()}>Retry loading project policy</button> : <p role="status">Loading project policy…</p>)}
    {validationError && <InlineValidationNotice message={validationError} />}
    <div className="runner-layout policy-layout">
      <form className="panel policy-form" onSubmit={(event) => void saveRuntime(event)}>
        <header className="panel-header compact"><div><h3>Command runtime</h3><p>Workspace commands never need a target address.</p></div><TerminalSquare size={18} /></header>
        <fieldset className="policy-form-body" aria-label="Command runtime settings" disabled={!policyReady || Boolean(saving) || previewMode}>
          <label>Execution mode<select aria-label="Project execution mode" disabled={!policy || saving === "runtime"} value={executionMode} onChange={(event) => { setExecutionMode(event.target.value as "docker" | "host"); setHostAcknowledged(false); }}><option value="docker">Docker mode</option><option value="host">Host mode</option></select><small>{executionMode === "docker" ? "Commands run inside Docker with the project folder mounted at /workspace." : "Commands run directly on the Nebula server as its OS user, starting in the project folder."}</small></label>
          {executionMode === "host" && <label className="provider-consent"><input type="checkbox" checked={hostAcknowledged} onChange={(event) => setHostAcknowledged(event.target.checked)} /><span><strong>Allow host filesystem and network access</strong><small>Agents can access and modify other host folders and mounted devices permitted by the server's OS account. Docker isolation, scope-filtered networking and container VPN routing do not apply. Command approvals and timeouts still apply. External MCP services keep their configured location.</small></span></label>}
          <label>Approval policy<select aria-describedby="command-approval-scope" value={approvalPolicy} onChange={(event) => setApprovalPolicy(event.target.value as AutomationProjectPolicy["approvalPolicy"])}><option value="on_boundary">On boundary · prompt once for project networking</option><option value="always">Always · prompt before every command</option><option value="never">Never · no command prompts</option></select></label>
          <p id="command-approval-scope" className="provider-dialog-note">Applies to Nebula's command runtime for new sessions. Harness, MCP and browser permissions are separate.</p>
          {approvalPolicy === "never" && <InlineValidationNotice message="Commands run without per-command approval; scope and other permission checks still apply." />}
          <label>Maximum command timeout (milliseconds)<input type="number" min={1000} max={86400000} value={maxTimeoutMs} onChange={(event) => setMaxTimeoutMs(Number(event.target.value))} /></label>
          {executionMode === "docker" && <><label className="provider-consent"><input type="checkbox" checked={networkEnabled} onChange={(event) => setNetworkEnabled(event.target.checked)} /><span><strong>Make project-scoped networking available</strong><small>The session receives the complete validated CIDR/domain/port policy. An approval never expands that scope.</small></span></label>
          <label>VPN route<select value={vpnProfileId} disabled={!networkEnabled} onChange={(event) => setVpnProfileId(event.target.value)}><option value="">Direct, scope-filtered egress</option>{vpnProfiles.map((profile) => <option key={profile.id} value={profile.id} disabled={!profile.available}>{profile.name} · {profile.protocol.toUpperCase()} {profile.remoteHost}</option>)}</select><small>{vpnProfileId ? "New command sessions must establish this tunnel before network access is released." : "Select a saved profile to route authorized container traffic through OpenVPN."}</small></label></>}
          <footer><span>Existing sessions keep their frozen policy revision.</span><button className="button primary" type="submit" disabled={previewMode || !policy || saving === "runtime" || (executionMode === "host" && !hostAcknowledged)}><Save size={14} /> {saving === "runtime" ? "Saving…" : "Save runtime policy"}</button></footer>
        </fieldset>
      </form>
      <form className="panel policy-form" onSubmit={(event) => void saveScope(event)}>
        <header className="panel-header compact"><div><h3>Network scope</h3><p>DNS plus TCP egress only; URL paths alone cannot authorize shell networking.</p></div><ShieldCheck size={18} /></header>
        <fieldset className="policy-form-body" aria-label="Network scope settings" disabled={!policyReady || Boolean(saving) || previewMode}>
          <label className="provider-consent"><input type="checkbox" checked={allowAllTargets} onChange={(event) => setAllowAllTargets(event.target.checked)} /><span><strong>All targets and ports</strong><small>Unrestricted target mode for authorized operators. Time windows, prohibited actions, privacy controls, and high-risk approvals remain enforced.</small></span></label>
          {allowAllTargets && <InlineValidationNotice message="All-targets mode overrides the destination and port allowlists below. Their saved values are retained and become authoritative again when this mode is turned off." />}
          <label>Allowed domains<textarea rows={4} value={allowedDomains} placeholder="example.com\nhttps://www.example.org" disabled={allowAllTargets} onChange={(event) => setAllowedDomains(event.target.value)} /><small>Hostnames and root HTTP(S) URLs are equivalent here. Paths belong in URL-only scope.</small></label>
          <label>Allowed CIDRs<textarea rows={4} value={allowedCidrs} placeholder="203.0.113.0/24" disabled={allowAllTargets} onChange={(event) => setAllowedCidrs(event.target.value)} /></label>
          <label>Allowed TCP ports<input value={allowedPorts} placeholder="80, 443, 8000-8100" disabled={allowAllTargets} onChange={(event) => setAllowedPorts(event.target.value)} /></label>
          <label>URL-only scope entries<textarea rows={3} value={allowedUrls} placeholder="https://example.com/reviewed/path" disabled={allowAllTargets} onChange={(event) => setAllowedUrls(event.target.value)} /></label>
          <div className="resource-form-grid"><label>Active from<input type="datetime-local" value={notBefore} onChange={(event) => setNotBefore(event.target.value)} /></label><label>Expires<input type="datetime-local" value={notAfter} onChange={(event) => setNotAfter(event.target.value)} /></label></div>
          <label>Prohibited actions<textarea rows={3} value={prohibitedActions} onChange={(event) => setProhibitedActions(event.target.value)} /></label>
          <div className="resource-form-grid"><label>Maximum concurrency<input type="number" min={1} max={256} value={maxConcurrency} onChange={(event) => setMaxConcurrency(Number(event.target.value))} /></label><label className="provider-consent"><input type="checkbox" checked={localOnly} onChange={(event) => setLocalOnly(event.target.checked)} /><span><strong>Local only</strong><small>Do not send project data to remote models.</small></span></label></div>
          <footer><span>Private and link-local destinations require an explicit CIDR.</span><button className="button primary" type="submit" disabled={previewMode || !scope || saving === "scope"}><Save size={14} /> {saving === "scope" ? "Saving…" : "Save scope"}</button></footer>
        </fieldset>
      </form>
    </div>
  </section>;
}
