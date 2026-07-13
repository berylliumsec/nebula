import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Clock3, Save, ShieldAlert, ShieldCheck, Wrench, X } from "lucide-react";
import { ApiError } from "../api/client";
import type { EngagementScopePolicy, EngagementToolAssignment, MissionGrant, ToolPackInstallation, ToolSummary } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { useToolPackRevision } from "../state/toolPackChanges";

function featureMissing(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 501);
}

function lines(value: string): string[] {
  return [...new Set(value.split(/[\n,]+/).map((item) => item.trim()).filter(Boolean))];
}

function inputDate(value?: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.valueOf() - offset).toISOString().slice(0, 16);
}

function wireDate(value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? undefined : date.toISOString();
}

function emptyScope(engagementId: string): EngagementScopePolicy {
  return { engagementId, allowedCidrs: [], allowedDomains: [], allowedUrls: [], allowedPorts: [], prohibitedActions: [], localOnly: true, maxConcurrency: 1, grants: [], revision: 0 };
}

function emptyAssignment(engagementId: string): EngagementToolAssignment {
  return { engagementId, toolNames: [], enabled: true, revision: 0 };
}

const grantRiskOptions = ["active_scan", "workspace_write", "credential_use", "exploitation", "persistence", "destructive"] as const;
const implicitEnvironmentTools = new Set(["environment.shell_local", "environment.shell_network"]);

export function EngagementPolicySettings() {
  const { activeOperator, api, coreState, engagement, previewMode } = useWorkspace();
  const [scope, setScope] = useState<EngagementScopePolicy>();
  const [assignment, setAssignment] = useState<EngagementToolAssignment>();
  const [assignments, setAssignments] = useState<EngagementToolAssignment[]>([]);
  const [packs, setPacks] = useState<ToolPackInstallation[]>([]);
  const [tools, setTools] = useState<ToolSummary[]>([]);
  const [scopeAvailable, setScopeAvailable] = useState(true);
  const [assignmentAvailable, setAssignmentAvailable] = useState(true);
  const [allowedCidrs, setAllowedCidrs] = useState("");
  const [allowedDomains, setAllowedDomains] = useState("");
  const [allowedUrls, setAllowedUrls] = useState("");
  const [allowedPorts, setAllowedPorts] = useState("");
  const [notBefore, setNotBefore] = useState("");
  const [notAfter, setNotAfter] = useState("");
  const [prohibitedActions, setProhibitedActions] = useState("");
  const [localOnly, setLocalOnly] = useState(true);
  const [maxConcurrency, setMaxConcurrency] = useState(1);
  const [grants, setGrants] = useState<MissionGrant[]>([]);
  const [selectedDigest, setSelectedDigest] = useState("");
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [assignmentEnabled, setAssignmentEnabled] = useState(false);
  const [saving, setSaving] = useState<"scope" | "assignment">();
  const [error, setError] = useState<string>();
  const [grantOpen, setGrantOpen] = useState(false);
  const [grantRiskClasses, setGrantRiskClasses] = useState<string[]>(["exploitation"]);
  const [grantTools, setGrantTools] = useState<string[]>([]);
  const [grantTargets, setGrantTargets] = useState("");
  const [grantExpires, setGrantExpires] = useState("");
  const [grantError, setGrantError] = useState<string>();
  const toolPackRevision = useToolPackRevision();

  const applyScope = (next: EngagementScopePolicy) => {
    setScope(next); setAllowedCidrs(next.allowedCidrs.join("\n")); setAllowedDomains(next.allowedDomains.join("\n")); setAllowedUrls(next.allowedUrls.join("\n")); setAllowedPorts(next.allowedPorts.join(", ")); setNotBefore(inputDate(next.notBefore)); setNotAfter(inputDate(next.notAfter)); setProhibitedActions(next.prohibitedActions.join("\n")); setLocalOnly(next.localOnly); setMaxConcurrency(next.maxConcurrency); setGrants(next.grants);
  };

  const applyAssignment = (next: EngagementToolAssignment) => {
    setAssignment(next); setSelectedDigest(next.manifestDigest ?? ""); setSelectedTools(next.toolNames.filter((name) => !implicitEnvironmentTools.has(name))); setAssignmentEnabled(next.enabled);
  };

  const load = useCallback(async () => {
    if (!api || coreState !== "online" || !engagement) return;
    setError(undefined);
    const [scopeResult, assignmentResult, packsResult, toolsResult] = await Promise.allSettled([
      api.getEngagementScope(engagement.id), api.listEngagementToolAssignments(engagement.id), api.listToolPacks(), api.listTools(),
    ]);
    if (scopeResult.status === "fulfilled") { setScopeAvailable(true); applyScope(scopeResult.value); }
    else if (featureMissing(scopeResult.reason)) { setScopeAvailable(false); applyScope(emptyScope(engagement.id)); }
    else setError(scopeResult.reason instanceof Error ? scopeResult.reason.message : "Could not load the engagement scope.");
    if (assignmentResult.status === "fulfilled") { setAssignmentAvailable(true); setAssignments(assignmentResult.value); }
    else if (featureMissing(assignmentResult.reason)) { setAssignmentAvailable(false); setAssignments([]); applyAssignment(emptyAssignment(engagement.id)); }
    else setError(assignmentResult.reason instanceof Error ? assignmentResult.reason.message : "Could not load tool assignments.");
    if (packsResult.status === "fulfilled") setPacks(packsResult.value);
    if (toolsResult.status === "fulfilled") setTools(toolsResult.value);
  }, [api, coreState, engagement?.id]);

  useEffect(() => { void load(); }, [load, toolPackRevision]);

  const readyPacks = useMemo(() => packs.filter((pack) => pack.status === "ready"), [packs]);
  const selectedPack = readyPacks.find((pack) => pack.manifestDigest === selectedDigest);
  const readyDigests = useMemo(() => new Set(readyPacks.map((pack) => pack.manifestDigest)), [readyPacks]);
  const packTools = selectedPack
    ? tools.filter((tool) => tool.packManifestDigest === selectedDigest || tool.packId === selectedPack.id)
    : [];
  const configurableTools = packTools.filter((tool) => !implicitEnvironmentTools.has(tool.name));
  const grantableToolNames = useMemo(() => [...new Set(assignments
    .filter((item) => item.enabled && item.manifestDigest !== undefined && readyDigests.has(item.manifestDigest))
    .flatMap((item) => item.toolNames)
    .filter((name) => !implicitEnvironmentTools.has(name)))].sort(), [assignments, readyDigests]);

  useEffect(() => {
    if (selectedDigest && readyDigests.has(selectedDigest)) return;
    const assignedReadyDigest = assignments
      .map((item) => item.manifestDigest)
      .find((digest): digest is string => digest !== undefined && readyDigests.has(digest));
    setSelectedDigest(assignedReadyDigest ?? readyPacks[0]?.manifestDigest ?? "");
  }, [assignments, readyDigests, readyPacks, selectedDigest]);

  useEffect(() => {
    if (!engagement) return;
    if (!selectedDigest) {
      setAssignment(emptyAssignment(engagement.id));
      setSelectedTools([]);
      setAssignmentEnabled(true);
      return;
    }
    applyAssignment(assignments.find((item) => item.manifestDigest === selectedDigest)
      ?? { ...emptyAssignment(engagement.id), manifestDigest: selectedDigest });
  }, [assignments, engagement?.id, selectedDigest]);

  const saveScope = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !engagement || !scope) return;
    const ports = lines(allowedPorts).map(Number);
    if (ports.some((port) => !Number.isInteger(port) || port < 1 || port > 65_535)) { setError("Allowed ports must be integers from 1 through 65535."); return; }
    const start = wireDate(notBefore); const end = wireDate(notAfter);
    if (start && end && start >= end) { setError("The scope end time must be after its start time."); return; }
    setSaving("scope"); setError(undefined);
    try {
      const next = await api.updateEngagementScope(engagement.id, { allowedCidrs: lines(allowedCidrs), allowedDomains: lines(allowedDomains), allowedUrls: lines(allowedUrls), allowedPorts: ports, notBefore: start, notAfter: end, prohibitedActions: lines(prohibitedActions), localOnly, maxConcurrency, grants, expectedRevision: scope.revision });
      applyScope(next);
    } catch (saveError) { setError(saveError instanceof Error ? saveError.message : "Could not save the engagement scope."); }
    finally { setSaving(undefined); }
  };

  const saveAssignment = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !engagement || !assignment || !selectedDigest) return;
    setSaving("assignment"); setError(undefined);
    try {
      const next = await api.updateEngagementToolAssignment(engagement.id, { manifestDigest: selectedDigest, toolNames: selectedTools, enabled: assignmentEnabled, expectedRevision: assignment.revision });
      setAssignments((current) => [next, ...current.filter((item) => item.manifestDigest !== next.manifestDigest)]);
      applyAssignment(next);
    } catch (saveError) { setError(saveError instanceof Error ? saveError.message : "Could not save the tool assignment."); }
    finally { setSaving(undefined); }
  };

  const addGrant = () => {
    const expiresAt = wireDate(grantExpires);
    if (!expiresAt || expiresAt <= new Date().toISOString()) { setGrantError("A mission grant needs a future expiration time."); return; }
    const now = new Date().toISOString();
    setGrants((current) => [...current, { riskClasses: grantRiskClasses, toolNames: grantTools, targets: lines(grantTargets), grantedAt: now, expiresAt, grantedBy: activeOperator?.id ?? "local-operator" }]);
    setGrantOpen(false); setGrantRiskClasses(["exploitation"]); setGrantTargets(""); setGrantTools([]); setGrantExpires(""); setGrantError(undefined); setError(undefined);
  };

  if (!engagement) return <section className="settings-section" id="engagement-policy-settings"><div className="empty-state compact"><ShieldAlert size={22} /><strong>No engagement selected</strong><p>Select an engagement before defining its scope or assigning tools.</p></div></section>;

  return <section className="settings-section" id="engagement-policy-settings"><div className="section-heading"><div><h2>Engagement policy</h2><p>Exact rules of engagement and environment access for {engagement.name}.</p></div><span className="policy-revision">Scope revision {scope?.revision ?? 0}</span></div>{error && <div className="knowledge-status error" role="alert">{error}</div>}<div className="policy-editor-grid"><form className="panel policy-form" onSubmit={(event) => void saveScope(event)}><header className="panel-header compact"><div><h3>Rules of engagement</h3><p>Core enforces these values before any network request.</p></div><ShieldCheck size={18} /></header>{!scopeAvailable ? <div className="feature-unavailable" role="status"><ShieldAlert size={21} /><div><strong>Scope editing is unavailable</strong><p>This Core build keeps all missions analysis-only.</p></div></div> : <div className="policy-form-body"><div className="resource-form-grid"><label>Allowed CIDRs<textarea rows={3} value={allowedCidrs} placeholder="192.0.2.0/24" onChange={(event) => setAllowedCidrs(event.target.value)} /></label><label>Allowed domains<textarea rows={3} value={allowedDomains} placeholder="app.example.test" onChange={(event) => setAllowedDomains(event.target.value)} /></label></div><label>Allowed URLs<textarea rows={2} value={allowedUrls} placeholder="https://app.example.test/api" onChange={(event) => setAllowedUrls(event.target.value)} /></label><label>Allowed ports<input value={allowedPorts} placeholder="80, 443, 8443" inputMode="numeric" onChange={(event) => setAllowedPorts(event.target.value)} /></label><div className="resource-form-grid"><label>Not before<input type="datetime-local" value={notBefore} onChange={(event) => setNotBefore(event.target.value)} /></label><label>Not after<input type="datetime-local" value={notAfter} onChange={(event) => setNotAfter(event.target.value)} /></label></div><label>Prohibited actions<textarea rows={3} value={prohibitedActions} placeholder="credential use&#10;exploitation&#10;destructive changes" onChange={(event) => setProhibitedActions(event.target.value)} /></label><div className="resource-form-grid"><label>Maximum concurrency<input type="number" min={1} max={10} value={maxConcurrency} onChange={(event) => setMaxConcurrency(Number(event.target.value))} /></label><label className="provider-consent"><input type="checkbox" checked={localOnly} onChange={(event) => setLocalOnly(event.target.checked)} /><span><strong>Local-only processing</strong><small>Prevent cloud providers from receiving engagement data.</small></span></label></div><section className="mission-grants"><header><div><strong>Optional high-risk grants</strong><small>Ordinary in-scope Toolbox scans do not need a grant; invasive extension capabilities still do.</small></div><button className="button quiet" type="button" onClick={() => { setGrantError(undefined); setGrantOpen(true); }}>Add grant</button></header>{grants.length ? grants.map((grant, index) => <article key={`${grant.grantedAt}-${index}`}><Clock3 size={14} /><div><strong>{grant.riskClasses.join(", ") || "No risk classes"}</strong><small>{grant.toolNames.join(", ") || "All assigned capabilities"} · expires {new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(grant.expiresAt))}</small></div><button className="icon-button subtle" type="button" aria-label="Remove mission grant" onClick={() => setGrants((current) => current.filter((_, itemIndex) => itemIndex !== index))}><X size={13} /></button></article>) : <p>No optional high-risk grants configured.</p>}</section><footer><span>Saving creates a revision-safe policy update.</span><button className="button primary" type="submit" disabled={previewMode || saving === "scope"}><Save size={14} /> {saving === "scope" ? "Saving…" : "Save scope"}</button></footer></div>}</form><form className="panel policy-form assignment-form" onSubmit={(event) => void saveAssignment(event)}><header className="panel-header compact"><div><h3>Execution-environment assignment</h3><p>Installation alone never grants an agent access to an environment.</p></div><Wrench size={18} /></header>{!assignmentAvailable ? <div className="feature-unavailable" role="status"><Wrench size={21} /><div><strong>Environment assignments are unavailable</strong><p>Missions remain analysis-only.</p></div></div> : <div className="policy-form-body"><label>Installed environment<select aria-label="Assigned execution environment" value={selectedDigest} disabled={!readyPacks.length} onChange={(event) => { setSelectedDigest(event.target.value); setSelectedTools([]); }}>{readyPacks.length ? readyPacks.map((pack) => <option value={pack.manifestDigest} key={pack.id}>{pack.publisher}/{pack.name} · {pack.version}</option>) : <option value="">No verified environments</option>}</select></label>{selectedDigest && <code className="digest-lock" title={selectedDigest}>{selectedDigest}</code>}{selectedDigest && <p>Local shell access and scoped-network shell access are included automatically when declared by this environment.</p>}<fieldset className="resource-checklist"><legend>Optional environment capabilities</legend>{configurableTools.length ? configurableTools.map((tool) => <label key={tool.name}><input type="checkbox" checked={selectedTools.includes(tool.name)} disabled={!tool.available} onChange={(event) => setSelectedTools((current) => event.target.checked ? [...current, tool.name] : current.filter((name) => name !== tool.name))} /><span><strong>{tool.name}</strong><small>{tool.riskClass.replaceAll("_", " ")}{tool.unavailableReason ? ` · ${tool.unavailableReason}` : ""}</small></span></label>) : <p>{readyPacks.length ? "No optional capabilities are exposed by this environment." : "Install and verify an environment first."}</p>}</fieldset><label className="provider-consent"><input type="checkbox" checked={assignmentEnabled} onChange={(event) => setAssignmentEnabled(event.target.checked)} /><span><strong>Enable this assignment</strong><small>The exact image manifest digest, automatic shell access, and checked optional capabilities become available.</small></span></label><footer><span>Assignment revision {assignment?.revision ?? 0}</span><button className="button primary" type="submit" disabled={previewMode || !selectedDigest || saving === "assignment"}><Save size={14} /> {saving === "assignment" ? "Saving…" : "Save assignment"}</button></footer></div>}</form></div>{grantOpen && <div className="dialog-backdrop"><section className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="grant-dialog-title"><header><div><small>Explicit authorization</small><h2 id="grant-dialog-title">Add high-risk grant</h2></div><button className="icon-button subtle" type="button" aria-label="Close mission grant dialog" onClick={() => setGrantOpen(false)}><X size={16} /></button></header><fieldset className="resource-checklist"><legend>Risk classes</legend>{grantRiskOptions.map((risk) => <label key={risk}><input type="checkbox" checked={grantRiskClasses.includes(risk)} onChange={(event) => setGrantRiskClasses((current) => event.target.checked ? [...current, risk] : current.filter((item) => item !== risk))} /><span>{risk.replaceAll("_", " ")}</span></label>)}</fieldset><fieldset className="resource-checklist"><legend>Capabilities</legend>{grantableToolNames.map((name) => <label key={name}><input type="checkbox" checked={grantTools.includes(name)} onChange={(event) => setGrantTools((current) => event.target.checked ? [...current, name] : current.filter((item) => item !== name))} /><span>{name}</span></label>)}{!grantableToolNames.length && <p>Save and enable an environment assignment first.</p>}</fieldset><label>Targets<textarea rows={3} value={grantTargets} placeholder="192.0.2.10&#10;https://app.example.test" onChange={(event) => setGrantTargets(event.target.value)} /></label><label>Expires<input type="datetime-local" value={grantExpires} onChange={(event) => setGrantExpires(event.target.value)} /></label>{grantError && <p className="form-error" role="alert">{grantError}</p>}<p className="provider-dialog-note">The grant is attributed to {activeOperator?.displayName ?? "the active local operator"}. It cannot expand the engagement scope.</p><footer><button className="button secondary" type="button" onClick={() => setGrantOpen(false)}>Cancel</button><button className="button primary" type="button" disabled={!grantExpires || !grantTargets.trim() || !grantRiskClasses.length} onClick={addGrant}>Add grant</button></footer></section></div>}</section>;
}
