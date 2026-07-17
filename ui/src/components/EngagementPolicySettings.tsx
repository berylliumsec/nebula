import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Save, ShieldCheck, TerminalSquare } from "lucide-react";
import type { AutomationProjectPolicy, EngagementScopePolicy } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { announceSettingsSaved } from "./SettingsSaveFeedback";

function lines(value: string): string[] {
  return [...new Set(value.split(/[\n,]+/).map((item) => item.trim()).filter(Boolean))];
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
  const { api, coreState, engagement, previewMode } = useWorkspace();
  const [scope, setScope] = useState<EngagementScopePolicy>();
  const [policy, setPolicy] = useState<AutomationProjectPolicy>();
  const [allowedCidrs, setAllowedCidrs] = useState("");
  const [allowedDomains, setAllowedDomains] = useState("");
  const [allowedUrls, setAllowedUrls] = useState("");
  const [allowedPorts, setAllowedPorts] = useState("");
  const [notBefore, setNotBefore] = useState("");
  const [notAfter, setNotAfter] = useState("");
  const [prohibitedActions, setProhibitedActions] = useState("");
  const [localOnly, setLocalOnly] = useState(true);
  const [maxConcurrency, setMaxConcurrency] = useState(1);
  const [approvalPolicy, setApprovalPolicy] = useState<AutomationProjectPolicy["approvalPolicy"]>("on_boundary");
  const [networkEnabled, setNetworkEnabled] = useState(false);
  const [maxTimeoutMs, setMaxTimeoutMs] = useState(300_000);
  const [saving, setSaving] = useState<"scope" | "runtime">();
  const [error, setError] = useState<unknown>();

  const applyScope = (next: EngagementScopePolicy) => {
    setScope(next);
    setAllowedCidrs(next.allowedCidrs.join("\n"));
    setAllowedDomains(next.allowedDomains.join("\n"));
    setAllowedUrls(next.allowedUrls.join("\n"));
    setAllowedPorts(next.allowedPorts.join(", "));
    setNotBefore(inputDate(next.notBefore));
    setNotAfter(inputDate(next.notAfter));
    setProhibitedActions(next.prohibitedActions.join("\n"));
    setLocalOnly(next.localOnly);
    setMaxConcurrency(next.maxConcurrency);
  };

  const applyPolicy = (next: AutomationProjectPolicy) => {
    setPolicy(next);
    setApprovalPolicy(next.approvalPolicy);
    setNetworkEnabled(next.networkEnabled);
    setMaxTimeoutMs(next.maxTimeoutMs);
  };

  const load = useCallback(async () => {
    if (!api || coreState !== "online" || !engagement) return;
    setError(undefined);
    try {
      const [nextScope, nextPolicy] = await Promise.all([
        api.getEngagementScope(engagement.id),
        api.getAutomationPolicy(engagement.id),
      ]);
      applyScope(nextScope);
      applyPolicy(nextPolicy);
    } catch (loadError) {
      void logCaughtDiagnostic("interface.execution_policy.caught_failure_01", "A handled interface operation failed.", loadError, "execution_policy");
      setError(loadError instanceof Error ? loadError.message : "Could not load project execution policy.");
    }
  }, [api, coreState, engagement?.id]);

  useEffect(() => { void load(); }, [load]);

  const saveScope = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !engagement || !scope) return;
    const ports = lines(allowedPorts).map(Number);
    if (ports.some((port) => !Number.isInteger(port) || port < 1 || port > 65_535)) {
      setError("Allowed ports must be integers from 1 through 65535.");
      return;
    }
    const start = wireDate(notBefore);
    const end = wireDate(notAfter);
    if (start && end && start >= end) {
      setError("The scope end time must be after its start time.");
      return;
    }
    setSaving("scope"); setError(undefined);
    try {
      applyScope(await api.updateEngagementScope(engagement.id, {
        allowedCidrs: lines(allowedCidrs),
        allowedDomains: lines(allowedDomains),
        allowedUrls: lines(allowedUrls),
        allowedPorts: ports,
        notBefore: start,
        notAfter: end,
        prohibitedActions: lines(prohibitedActions),
        localOnly,
        maxConcurrency,
        grants: scope.grants,
        expectedRevision: scope.revision,
      }));
      announceSettingsSaved("Network scope updated for new sessions.");
    } catch (saveError) {
      void logCaughtDiagnostic("interface.engagement_policy.scope_save_failed", "Project scope could not be saved.", saveError, "engagement_policy");
      setError(saveError);
    } finally { setSaving(undefined); }
  };

  const saveRuntime = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !engagement || !policy) return;
    setSaving("runtime"); setError(undefined);
    try {
      applyPolicy(await api.updateAutomationPolicy(engagement.id, {
        approvalPolicy,
        networkEnabled,
        runnerProfileId: policy.runnerProfileId,
        maxTimeoutMs,
        expectedRevision: policy.revision,
      }));
      announceSettingsSaved("Runtime policy updated for new sessions.");
    } catch (saveError) {
      void logCaughtDiagnostic("interface.engagement_policy.runtime_save_failed", "Project command-runtime policy could not be saved.", saveError, "engagement_policy");
      setError(saveError);
    } finally { setSaving(undefined); }
  };

  return <section className="settings-section" id="engagement-policy-settings">
    <div className="section-heading"><div><h2>Project execution policy</h2><p>Freeze the scope, approval behavior, and whole-project network boundary used by new agent sessions.</p></div><ShieldCheck size={20} /></div>
    {Boolean(error) && <DiagnosticErrorNotice error={error} fallback="The project policy could not be updated." compact />}
    <div className="runner-layout">
      <form className="panel policy-form" onSubmit={(event) => void saveRuntime(event)}>
        <header className="panel-header compact"><div><h3>Command runtime</h3><p>Workspace commands never need a target address.</p></div><TerminalSquare size={18} /></header>
        <label>Approval policy<select value={approvalPolicy} onChange={(event) => setApprovalPolicy(event.target.value as AutomationProjectPolicy["approvalPolicy"])}><option value="on_boundary">On boundary · prompt once for project networking</option><option value="always">Always · prompt before every command</option><option value="never">Never · run without prompts</option></select></label>
        <label>Maximum command timeout (milliseconds)<input type="number" min={1000} max={86400000} value={maxTimeoutMs} onChange={(event) => setMaxTimeoutMs(Number(event.target.value))} /></label>
        <label className="provider-consent"><input type="checkbox" checked={networkEnabled} onChange={(event) => setNetworkEnabled(event.target.checked)} /><span><strong>Make project-scoped networking available</strong><small>The session receives the complete validated CIDR/domain/port policy. An approval never expands that scope.</small></span></label>
        <footer><span>Existing sessions keep their frozen policy revision.</span><button className="button primary" type="submit" disabled={previewMode || !policy || saving === "runtime"}><Save size={14} /> {saving === "runtime" ? "Saving…" : "Save runtime policy"}</button></footer>
      </form>
      <form className="panel policy-form" onSubmit={(event) => void saveScope(event)}>
        <header className="panel-header compact"><div><h3>Network scope</h3><p>DNS plus TCP egress only; URL paths alone cannot authorize shell networking.</p></div><ShieldCheck size={18} /></header>
        <label>Allowed domains<textarea rows={4} value={allowedDomains} placeholder="example.com\n*.example.org" onChange={(event) => setAllowedDomains(event.target.value)} /></label>
        <label>Allowed CIDRs<textarea rows={4} value={allowedCidrs} placeholder="203.0.113.0/24" onChange={(event) => setAllowedCidrs(event.target.value)} /></label>
        <label>Allowed TCP ports<input value={allowedPorts} placeholder="80, 443" onChange={(event) => setAllowedPorts(event.target.value)} /></label>
        <label>URL-only scope entries<textarea rows={3} value={allowedUrls} placeholder="https://example.com/reviewed/path" onChange={(event) => setAllowedUrls(event.target.value)} /></label>
        <div className="resource-form-grid"><label>Active from<input type="datetime-local" value={notBefore} onChange={(event) => setNotBefore(event.target.value)} /></label><label>Expires<input type="datetime-local" value={notAfter} onChange={(event) => setNotAfter(event.target.value)} /></label></div>
        <label>Prohibited actions<textarea rows={3} value={prohibitedActions} onChange={(event) => setProhibitedActions(event.target.value)} /></label>
        <div className="resource-form-grid"><label>Maximum concurrency<input type="number" min={1} max={256} value={maxConcurrency} onChange={(event) => setMaxConcurrency(Number(event.target.value))} /></label><label className="provider-consent"><input type="checkbox" checked={localOnly} onChange={(event) => setLocalOnly(event.target.checked)} /><span><strong>Local only</strong><small>Do not send project data to remote models.</small></span></label></div>
        <footer><span>Private and link-local destinations require an explicit CIDR.</span><button className="button primary" type="submit" disabled={previewMode || !scope || saving === "scope"}><Save size={14} /> {saving === "scope" ? "Saving…" : "Save scope"}</button></footer>
      </form>
    </div>
  </section>;
}
