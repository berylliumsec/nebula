import { useEffect, useId, useRef, useState } from "react";
import { ArrowRight, ChevronDown, ChevronUp, RefreshCw } from "lucide-react";
import { ApiError, type ApiClient } from "../api/client";
import type { ButlerAction, ButlerAuthority, ButlerAuthorityPolicy, ButlerDecision, ButlerIncident, ButlerOverview, ButlerRecoveryCase, ButlerResource, ButlerWatchedGoal } from "../api/butlerOverview";
import { acknowledgeButlerIncident, butlerResourceKey, getButlerAuthority, updateButlerAuthority } from "../api/butlerOverview";
import { logCaughtDiagnostic } from "../diagnostics";
import { useButlerOverview } from "./useButlerOverview";
import { ButlerIssues } from "./ButlerIssues";
import styles from "./ButlerOversight.module.css";

function timeLabel(value?: string): string | undefined {
  if (!value) return undefined;
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) return undefined;
  return time.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function workUrl(projectId: string, sessionId?: string, missionId?: string): string | undefined {
  if (missionId) return `/projects/${encodeURIComponent(projectId)}/workbench?view=missions&mission=${encodeURIComponent(missionId)}`;
  return sessionId
    ? `/projects/${encodeURIComponent(projectId)}/workbench?view=chat&session=${encodeURIComponent(sessionId)}`
    : undefined;
}

function WorkLink({ projectId, sessionId, missionId, onNavigate, label = "Open chat" }: {
  projectId: string;
  sessionId?: string;
  missionId?: string;
  onNavigate?(path: string): void;
  label?: string;
}) {
  const href = workUrl(projectId, sessionId, missionId);
  if (!href) return null;
  return <a className={styles.workLink} href={href} onClick={(event) => {
    if (!onNavigate) return;
    event.preventDefault();
    onNavigate(href);
  }}>{label} <ArrowRight size={15} aria-hidden="true" /></a>;
}

function GoalRow({ goal, parentGoal, projectId, onNavigate }: {
  goal: ButlerWatchedGoal;
  parentGoal?: ButlerWatchedGoal;
  projectId: string;
  onNavigate?(path: string): void;
}) {
  const reportLabel = goal.coverage.reportCapability === "validated" ? "Structured agent reports"
    : goal.coverage.reportCapability === "turn_output_only" ? "Turn output only"
      : goal.coverage.reportCapability === "unavailable" ? "Goal lifecycle only"
        : "Reporting coverage unknown";
  return <li className={styles.row}>
    <div className={styles.rowHead}><strong>{goal.objective}</strong><span>{goal.actorRole === "subagent" ? "Subagent" : "Main agent"} · {goal.status.replaceAll("_", " ")}</span></div>
    {goal.actorRole === "subagent" && <small>Reports through main agent{parentGoal ? ` · ${parentGoal.objective}` : ""}</small>}
    <small>{reportLabel}{goal.coverage.reason ? ` · ${goal.coverage.reason}` : ""}</small>
    {goal.progressDueAt && <small>Next checkpoint due {timeLabel(goal.progressDueAt) ?? "at an unknown time"}</small>}
    <WorkLink projectId={projectId} sessionId={goal.sessionId} onNavigate={onNavigate} />
  </li>;
}

function IncidentRow({ incident, projectId, goals, onNavigate, onAcknowledge, acknowledging, canAcknowledge, compact = false }: {
  incident: ButlerIncident;
  projectId: string;
  goals: ButlerWatchedGoal[];
  onNavigate?(path: string): void;
  onAcknowledge(incident: ButlerIncident): void;
  acknowledging: boolean;
  canAcknowledge: boolean;
  compact?: boolean;
}) {
  const affectedGoals = goals.filter((goal) => incident.goalIds.includes(goal.goalId));
  const missionId = incident.evidenceRefs.find((ref) => ref.kind === "mission")?.id;
  return <li className={`${styles.row} ${compact ? styles.compactIncident : ""}`}>
    <div className={styles.rowHead}><strong>{incident.summary}</strong><span>{incident.status.replaceAll("_", " ")}</span></div>
    {!compact && <small>{incident.source} · {incident.verificationState.replaceAll("_", " ")}{incident.updatedAt ? ` · ${timeLabel(incident.updatedAt) ?? "Time unknown"}` : ""}</small>}
    {incident.impact && <p><b>Impact:</b> {incident.impact}</p>}
    {incident.nextAction && <p><b>Next:</b> {incident.nextAction}</p>}
    {!compact && incident.goalIds.length > 0 && <small>Affected goals: {affectedGoals.length ? affectedGoals.map((goal) => goal.objective).join(" · ") : incident.goalIds.length}</small>}
    {incident.evidenceSummary.length > 0 && <details className={styles.evidence}><summary>Agent-reported evidence · {incident.evidenceSummary.length}</summary><ul>{incident.evidenceSummary.map((item, index) => <li key={`${incident.id}-reported-${index}`}>{item}</li>)}</ul></details>}
    {!compact && incident.evidenceRefs.length > 0 && <details className={styles.evidence}><summary>Evidence references</summary><ul>{incident.evidenceRefs.map((item) => <li key={`${item.kind}-${item.id}`}>{item.kind.replaceAll("_", " ")} · <code>{item.id}</code></li>)}</ul></details>}
    <div className={styles.actions}>{incident.sessionIds.map((sessionId, index) => <WorkLink key={sessionId} projectId={projectId} sessionId={sessionId} onNavigate={onNavigate} label={incident.sessionIds.length > 1 ? `Open affected chat ${index + 1}` : "Open chat"} />)}
      {missionId && <WorkLink projectId={projectId} missionId={missionId} onNavigate={onNavigate} label="Open mission" />}
      {incident.status === "open" && <button className="button quiet" type="button" disabled={acknowledging || !canAcknowledge} title={!canAcknowledge ? "Reconnect to Core to acknowledge" : undefined} onClick={() => onAcknowledge(incident)}>{acknowledging ? "Acknowledging…" : "Acknowledge"}</button>}</div>
    {!canAcknowledge && incident.status === "open" && <small>Reconnect to Core to acknowledge this incident.</small>}
  </li>;
}

function DecisionRow({ decision, projectId, onNavigate }: {
  decision: ButlerDecision;
  projectId: string;
  onNavigate?(path: string): void;
}) {
  return <li className={styles.row}>
    <div className={styles.rowHead}><strong>{decision.summary}</strong>{decision.reviewedAt && <span>{timeLabel(decision.reviewedAt) ?? "Time unknown"}</span>}</div>
    {decision.choice && <p><b>Choice:</b> {decision.choice.replaceAll("_", " ")}{decision.status ? ` · ${decision.status.replaceAll("_", " ")}` : ""}</p>}
    {decision.reason && <p><b>Reason:</b> {decision.reason}</p>}
    {decision.impact && <p><b>Impact:</b> {decision.impact}</p>}
    {decision.nextAction && <p><b>Next:</b> {decision.nextAction}</p>}
    {decision.sourceVerificationState && <small>Source: {decision.sourceVerificationState.replaceAll("_", " ")}</small>}
    {decision.evidenceRefs.length > 0 && <details className={styles.evidence}><summary>Evidence references</summary><ul>{decision.evidenceRefs.map((item) => <li key={`${decision.id}-${item.kind}-${item.id}`}>{item.kind.replaceAll("_", " ")} · <code>{item.id}</code></li>)}</ul></details>}
    <WorkLink projectId={projectId} sessionId={decision.sessionId} onNavigate={onNavigate} />
  </li>;
}

function ActionRow({ action, projectId, onNavigate }: {
  action: ButlerAction;
  projectId: string;
  onNavigate?(path: string): void;
}) {
  const kind = action.actionType === "request_parent_status" ? "Parent status request" : "Urgent main-goal pause";
  const when = timeLabel(action.finishedAt ?? action.startedAt ?? action.createdAt);
  return <li className={`${styles.row} ${action.status === "uncertain" || action.status === "held" ? styles.actionUncertain : ""}`}>
    <div className={styles.rowHead}><strong>{action.summary}</strong><span>{action.status.replaceAll("_", " ")}</span></div>
    <small>{kind}{action.verificationState ? ` · ${action.verificationState.replaceAll("_", " ")}` : ""}{when ? ` · ${when}` : ""}</small>
    {action.status === "uncertain" && <p className={styles.actionWarning}>Core cannot confirm whether this action was delivered. Check the parent chat or goal before taking another action.</p>}
    {action.status === "held" && <p className={styles.actionWarning}>Parent guidance was held because immediate delivery was unavailable. Review the parent chat before sending it manually.</p>}
    <WorkLink projectId={projectId} sessionId={action.targetSessionId ?? action.sourceSessionId} onNavigate={onNavigate} label="Open parent chat" />
  </li>;
}

function RecoveryRow({ item, projectId, onNavigate }: {
  item: ButlerRecoveryCase;
  projectId: string;
  onNavigate?(path: string): void;
}) {
  const status = item.status.replaceAll("_", " ");
  const changed = item.steps.filter((step) => step.status === "applied");
  return <li className={`${styles.row} ${item.status === "uncertain" ? styles.actionUncertain : ""}`}>
    <div className={styles.rowHead}><strong>{item.summary}</strong><span>{status}</span></div>
    <small>{item.trigger === "immediate" ? "After a Core signal" : "Scheduled check"}{item.createdAt ? ` · ${timeLabel(item.createdAt) ?? "Time unknown"}` : ""}</small>
    {item.outcome && <p>{item.outcome}</p>}
    {changed.length > 0 && <small>{changed.length} action{changed.length === 1 ? "" : "s"} recorded</small>}
    {item.steps.length > 0 && <details className={styles.evidence}><summary>Investigation steps · {item.steps.length}</summary>
      <ul>{item.steps.map((step) => <li key={step.id}>{step.operation.replace("nebula.work.", "").replaceAll("_", " ")} · {step.status}</li>)}</ul>
    </details>}
    <WorkLink projectId={projectId} sessionId={item.targetSessionId ?? item.sourceSessionId} onNavigate={onNavigate} label="Open parent chat" />
  </li>;
}

function ResourceRow({ resource, onNavigate }: { resource: ButlerResource; onNavigate?(path: string): void }) {
  if (resource.kind === "harness_quota") {
    const measured = resource.status === "observed" && resource.remainingPercent !== undefined;
    const remaining = measured ? new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(resource.remainingPercent!) : undefined;
    return <li className={styles.row}>
      <div className={styles.rowHead}><strong>{resource.harnessName}</strong><span>Harness quota · {resource.windowLabel}</span></div>
      {measured ? <p><strong>{remaining}% remaining</strong>{resource.thresholdState === "at_or_below" && <span className={styles.resourceAttention}> · At or below 20% quota threshold</span>}</p>
        : <p>Quota remaining unknown. No numeric balance was reported.</p>}
      <small>{resource.source.replaceAll("_", " ")}{resource.observedAt ? ` · Observed ${timeLabel(resource.observedAt) ?? "at an unknown time"}` : ""}{resource.resetsAt ? ` · Resets ${timeLabel(resource.resetsAt) ?? "at an unknown time"}` : ""}</small>
    </li>;
  }
  const settingsPath = "/settings#provider-settings";
  const observed = resource.status === "observed" && resource.remainingUsd !== undefined;
  const balance = observed ? new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(resource.remainingUsd!) : undefined;
  return <li className={styles.row}>
    <div className={styles.rowHead}><strong>{resource.providerName}</strong><span>OpenRouter account</span></div>
    {observed ? <>
      <p><strong>{balance} remaining</strong>{resource.thresholdState === "at_or_below" && <span className={styles.resourceAttention}> · At or below account threshold</span>}</p>
      <small>Observed {timeLabel(resource.observedAt) ?? "at an unknown time"}. Balance may change between checks.</small>
    </> : <>
      <p>Account balance unavailable.</p>
      <small>Set up or check the separate OpenRouter management key in Model providers.</small>
    </>}
    <a className={styles.workLink} href={settingsPath} onClick={(event) => {
      if (!onNavigate) return;
      event.preventDefault();
      onNavigate(settingsPath);
    }}>Model providers <ArrowRight size={15} aria-hidden="true" /></a>
  </li>;
}

type AuthorityEditorState = {
  projectId: string;
  policy?: ButlerAuthorityPolicy;
  draftAuthority: ButlerAuthority;
  pauseUrgentMachineBlocker: boolean;
  loading: boolean;
  saving: boolean;
  error?: string;
  conflict?: boolean;
  saved?: boolean;
};

function AuthorityEditor({ api, projectId, coreState, overview, onSaved }: {
  api?: ApiClient;
  projectId: string;
  coreState: "checking" | "online" | "offline";
  overview: ButlerOverview;
  onSaved(): Promise<void>;
}) {
  const selectId = useId();
  const grantId = useId();
  const [open, setOpen] = useState(false);
  const [reload, setReload] = useState(0);
  const [editor, setEditor] = useState<AuthorityEditorState>();
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const current = editor?.projectId === projectId ? editor : undefined;
  const levels = overview.capabilities.authorityLevels;
  const editable = levels.length > 1;

  useEffect(() => {
    if (!open || !editable || !api || coreState !== "online") return;
    const controller = new AbortController();
    setEditor({ projectId, draftAuthority: "watch", pauseUrgentMachineBlocker: false, loading: true, saving: false });
    void getButlerAuthority(api, projectId, controller.signal).then((policy) => {
      if (!controller.signal.aborted) setEditor({ projectId, policy, draftAuthority: policy.authority,
        pauseUrgentMachineBlocker: policy.delegateActions.includes("pause_urgent_machine_blocker"), loading: false, saving: false });
    }).catch((caught) => {
      if (controller.signal.aborted) return;
      void logCaughtDiagnostic("interface.butler_oversight.authority_load_failed", "Butler authority could not be loaded.", caught, "butler_oversight");
      setEditor({ projectId, draftAuthority: "watch", pauseUrgentMachineBlocker: false,
        loading: false, saving: false, error: caught instanceof Error ? caught.message : "Could not load saved authority." });
    });
    return () => controller.abort();
  }, [api, coreState, editable, open, projectId, reload]);

  const save = async () => {
    if (!api || coreState !== "online" || !current?.policy || current.saving || current.conflict) return;
    const policy = current.policy;
    const authority = current.draftAuthority;
    const grants = (authority === "delegate" || authority === "act") && current.pauseUrgentMachineBlocker && policy.supportedActions.includes("pause_urgent_machine_blocker")
      ? ["pause_urgent_machine_blocker" as const] : [];
    setEditor((previous) => previous?.projectId === projectId ? { ...previous, saving: true, error: undefined, saved: false } : previous);
    try {
      const updated = await updateButlerAuthority(api, policy, authority, grants);
      if (!alive.current || updated.projectId !== projectId) return;
      setEditor({ projectId, policy: updated, draftAuthority: updated.authority,
        pauseUrgentMachineBlocker: updated.delegateActions.includes("pause_urgent_machine_blocker"), loading: false, saving: false, saved: true });
      await onSaved();
    } catch (caught) {
      if (!alive.current) return;
      void logCaughtDiagnostic("interface.butler_oversight.authority_save_failed", "Butler authority could not be saved.", caught, "butler_oversight");
      const conflict = caught instanceof ApiError && caught.status === 409;
      setEditor((previous) => previous?.projectId === projectId ? { ...previous, saving: false, conflict,
        error: conflict ? "Authority changed elsewhere. Reload the saved setting before trying again."
          : caught instanceof Error ? caught.message : "Could not save authority. Try again." } : previous);
    }
  };

  const supportedStatus = current?.policy?.supportedActions.includes("request_parent_status") ?? false;
  const supportedPause = current?.policy?.supportedActions.includes("pause_urgent_machine_blocker") ?? false;
  const description = current?.draftAuthority === "watch" ? "Watch tracks goal state and reports incidents."
    : current?.draftAuthority === "act" ? "Act investigates project agent problems, uses available controls to repair them, and checks the result. Actions appear in Oversight."
    : current?.draftAuthority === "steer" ? supportedStatus
      ? "Steer can request a status or clarification from the parent main agent after Core flags an overdue goal checkpoint."
      : "No automatic Steer actions are available in this build."
      : current?.pauseUrgentMachineBlocker && supportedPause
        ? "Delegate can request parent status and pause a running main goal after a reviewed, urgent agent-reported machine blocker."
        : supportedStatus ? "Delegate can request parent status. An urgent pause requires the separate grant below."
          : "No automatic Delegate actions are available in this build.";
  const savedGrant = current?.policy?.delegateActions.includes("pause_urgent_machine_blocker") ?? false;
  const dirty = current?.policy && (current.draftAuthority !== current.policy.authority
    || ((current.draftAuthority === "delegate" || current.draftAuthority === "act") && current.pauseUrgentMachineBlocker !== savedGrant));

  return <details className={styles.disclosure} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Oversight settings</summary>
    <div className={styles.settings}>
      {!editable ? <p><strong>Authority:</strong> Watch · Butler watches running goals automatically.</p> : <>
        {current?.loading && <p role="status" className={styles.note}>Loading saved authority…</p>}
        {current?.error && <div className={styles.error} role="alert"><span>{current.error}</span><button className="button quiet" type="button" disabled={coreState !== "online"} onClick={() => setReload((value) => value + 1)}>{current.conflict ? "Reload saved authority" : "Retry"}</button></div>}
        {current?.policy && <>
          <label className={styles.settingsLabel} htmlFor={selectId}>Butler authority</label>
          <select id={selectId} className={styles.authoritySelect} value={current.draftAuthority} disabled={coreState !== "online" || current.saving || current.conflict || !levels.includes(current.policy.authority)} onChange={(event) => {
            const authority = event.target.value as ButlerAuthority;
            setEditor((previous) => previous?.projectId === projectId ? { ...previous, draftAuthority: authority,
              pauseUrgentMachineBlocker: (authority === "delegate" || authority === "act") && previous.pauseUrgentMachineBlocker, error: undefined, saved: false } : previous);
          }}>{levels.map((level) => <option key={level} value={level}>{level[0].toUpperCase() + level.slice(1)}</option>)}</select>
          {!levels.includes(current.policy.authority) && <p className={styles.note}>The saved authority is unavailable in this build. Ask an administrator to review it.</p>}
          <p className={styles.note}>{description}</p>
          {(current.draftAuthority === "delegate" || current.draftAuthority === "act") && supportedPause && <label className={styles.grantLabel} htmlFor={grantId}>
            <input id={grantId} type="checkbox" checked={current.pauseUrgentMachineBlocker} disabled={coreState !== "online" || current.saving || current.conflict} onChange={(event) => setEditor((previous) => previous?.projectId === projectId ? { ...previous, pauseUrgentMachineBlocker: event.target.checked, saved: false } : previous)} />
            <span>Allow urgent pause of a running main goal after a reviewed, agent-reported machine blocker.</span>
          </label>}
          {current.draftAuthority === "delegate" && supportedPause && <p className={styles.note}>The pause is reversible and audited. Butler cannot approve pending requests in Delegate mode.</p>}
          {dirty && <button className="button quiet" type="button" disabled={coreState !== "online" || current.saving || current.conflict || !levels.includes(current.draftAuthority)} onClick={() => void save()}>{current.saving ? "Saving…" : "Save authority"}</button>}
          {current.saved && <p role="status" className={styles.note}>Authority saved for this project.</p>}
        </>}
        {coreState !== "online" && <p className={styles.note}>Reconnect to Core to change authority.</p>}
      </>}
      <p className={styles.note}>{overview.capabilities.machineAlerts ? "Agent-reported machine problems can create incidents here. Claims remain agent-reported until verified." : "Automatic machine incidents are not active for this project's current runtimes."}</p>
      {!overview.capabilities.resourceAlerts && <p className={styles.note}>Account and quota alerts are not active yet.</p>}
      {!overview.capabilities.liveEvents && <p className={styles.note}>This view checks for updates while open. Watch continues in Core when you close it.</p>}
      {overview.coverageGaps.length > 0 && <div><strong>Coverage gaps</strong><ul>{overview.coverageGaps.map((gap, index) => <li key={`${index}-${gap}`}>{gap}</li>)}</ul></div>}
    </div>
  </details>;
}

/** Standalone project oversight surface. Core's overview is the sole state authority. */
export function ButlerOversight({ api, projectId, coreState, onNavigate, onIncidentChanged, compact = false }: {
  api?: ApiClient;
  projectId: string;
  coreState: "checking" | "online" | "offline";
  onNavigate?(path: string): void;
  onIncidentChanged?(): void;
  compact?: boolean;
}) {
  const { overview, loading, loadingMore, error, refresh, loadMore } = useButlerOverview(api, projectId, coreState);
  const [expanded, setExpanded] = useState(!compact);
  const incidentsAvailable = overview?.capabilities.incidents ?? false;
  const decisionsAvailable = overview?.capabilities.decisions ?? false;
  const uncertainActions = overview?.actions.filter((action) => action.status === "uncertain" || action.status === "held") ?? [];
  const recoveryAttention = overview?.recoveryCases.filter((item) => item.status === "needs_attention" || item.status === "uncertain") ?? [];
  const recoveryActive = overview?.recoveryCases.filter((item) => ["observed", "investigating", "verifying"].includes(item.status)) ?? [];
  const recoverySettled = overview?.recoveryCases.filter((item) => !recoveryAttention.includes(item) && !recoveryActive.includes(item)) ?? [];
  const otherActions = overview?.actions.filter((action) => action.status !== "uncertain" && action.status !== "held") ?? [];
  const shownActions = [...uncertainActions, ...otherActions.slice(0, 3)];
  const olderActions = otherActions.slice(3);
  const canAcknowledge = !!api && coreState === "online";
  const selectedProject = useRef(projectId);
  selectedProject.current = projectId;
  const [acknowledging, setAcknowledging] = useState<{ projectId: string; incidentId: string }>();
  const [ackError, setAckError] = useState<{ projectId: string; message: string }>();
  const acknowledge = async (incident: ButlerIncident) => {
    if (!api || coreState !== "online") return;
    setAcknowledging({ projectId, incidentId: incident.id });
    setAckError(undefined);
    try {
      await acknowledgeButlerIncident(api, projectId, incident);
      if (selectedProject.current === projectId) {
        await refresh();
        onIncidentChanged?.();
      }
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        if (selectedProject.current === projectId) {
          await refresh();
          setAckError({ projectId, message: "This incident changed. Review its latest details, then acknowledge it." });
        }
        return;
      }
      void logCaughtDiagnostic("interface.butler_oversight.ack_failed", "Butler incident could not be acknowledged.", caught, "butler_oversight");
      if (selectedProject.current === projectId) setAckError({ projectId, message: caught instanceof Error ? caught.message : "Try again." });
    } finally {
      setAcknowledging((current) => current?.projectId === projectId && current.incidentId === incident.id ? undefined : current);
    }
  };

  if (compact && !expanded) {
    const openIncident = overview?.incidents.find((incident) => incident.status === "open");
    const openCount = overview?.summary.openIncidents ?? 0;
    const needsAttention = openCount + uncertainActions.length + (overview?.summary.recoveryAttention ?? 0);
    return <section className={`${styles.root} ${styles.compact}`} aria-label="Butler oversight">
      <button className={styles.compactToggle} type="button" aria-expanded={false} title="Open Butler oversight" onClick={() => setExpanded(true)}>
        <span>Oversight</span>
        <small className={needsAttention ? styles.attention : undefined}>{coreState !== "online" ? "Offline" : error ? "Needs refresh" : overview ? `${overview.summary.watchedGoals} watched · ${needsAttention} need attention` : loading ? "Loading…" : "Selected project"}</small>
        <ChevronDown size={17} aria-hidden="true" />
      </button>
      {ackError?.projectId === projectId && <p role="alert" className={styles.error}>Could not acknowledge incident. {ackError.message}</p>}
      {openIncident && <ul className={styles.list} aria-label="Open Butler incidents"><IncidentRow incident={openIncident} goals={overview?.watchedGoals ?? []} projectId={projectId} onNavigate={onNavigate} onAcknowledge={(incident) => void acknowledge(incident)} acknowledging={acknowledging?.projectId === projectId && acknowledging.incidentId === openIncident.id} canAcknowledge={canAcknowledge} compact /></ul>}
      {uncertainActions.length > 0 && <ul className={styles.list} aria-label="Uncertain Butler actions"><ActionRow action={uncertainActions[0]} projectId={projectId} onNavigate={onNavigate} /></ul>}
      {recoveryAttention.length > 0 && <ul className={styles.list} aria-label="Butler recovery needing attention"><RecoveryRow item={recoveryAttention[0]} projectId={projectId} onNavigate={onNavigate} /></ul>}
      {openCount > 1 && <button className={styles.moreIncidents} type="button" onClick={() => setExpanded(true)}>View all {openCount} open incidents</button>}
      {uncertainActions.length > 1 && <button className={styles.moreIncidents} type="button" onClick={() => setExpanded(true)}>View all {uncertainActions.length} uncertain actions</button>}
      {recoveryAttention.length > 1 && <button className={styles.moreIncidents} type="button" onClick={() => setExpanded(true)}>View all {recoveryAttention.length} recovery cases needing attention</button>}
    </section>;
  }

  return <section className={styles.root} aria-label="Butler oversight">
    <header className={styles.header}>
      <div><h2>Oversight</h2><small>{overview ? `${overview.summary.watchedGoals} watched goal${overview.summary.watchedGoals === 1 ? "" : "s"} · ${overview.authority[0].toUpperCase() + overview.authority.slice(1)}` : "Selected project"}</small></div>
      <div className={styles.headerActions}>
        <button className={styles.refresh} type="button" aria-label="Refresh Butler oversight" title="Refresh Butler oversight" disabled={loading || coreState !== "online"} onClick={() => void refresh()}><RefreshCw size={17} aria-hidden="true" /></button>
        {compact && <button className={styles.refresh} type="button" aria-label="Collapse Butler oversight" title="Collapse Butler oversight" onClick={() => setExpanded(false)}><ChevronUp size={17} aria-hidden="true" /></button>}
      </div>
    </header>
    {loading && !overview && <p role="status" className={styles.note}>Loading oversight…</p>}
    {loading && overview && <p role="status" className={styles.note}>Refreshing oversight…</p>}
    {coreState !== "online" && <p role="status" className={styles.note}>Core is offline. {overview ? "Showing the last overview from this project." : "Reconnect to see watched work."}</p>}
    {error && <div className={styles.error} role="alert"><span>Oversight could not be refreshed. {error}</span><button className="button quiet" type="button" disabled={coreState !== "online"} onClick={() => void refresh()}>Retry</button></div>}
    {overview && <>
      {overview.globalInstructions && <details className={styles.disclosure}>
        <summary>Nebula-wide Butler <span>{overview.globalInstructions.status === "ready" ? "Ready" : overview.globalInstructions.status === "missing" ? "No file" : overview.globalInstructions.status === "needs_act_host" ? "Needs Act" : overview.globalInstructions.status === "invalid" ? "Invalid file" : "Unavailable"}</span></summary>
        <p className={styles.note}>Butler checks once every 15 minutes, including idle projects, and chooses where to work. Changes require Act authority in the target project.</p>
        {overview.globalInstructions.path && <p className={styles.note}>Add your own instructions at <code>{overview.globalInstructions.path}</code>. Nebula does not create this file.</p>}
        {overview.globalInstructions.status === "needs_act_host" && <p className={styles.note}>Set one project to Act and configure its Butler chat to host the scheduled run.</p>}
        {overview.globalInstructions.detail && <p role="alert" className={styles.error}>{overview.globalInstructions.detail}</p>}
        {overview.globalInstructions.lastCheckAt && <p className={styles.note}>Last check: <time dateTime={overview.globalInstructions.lastCheckAt}>{new Date(overview.globalInstructions.lastCheckAt).toLocaleString()}</time></p>}
        {overview.globalInstructions.nextCheckAt && <p className={styles.note}>Next due: <time dateTime={overview.globalInstructions.nextCheckAt}>{new Date(overview.globalInstructions.nextCheckAt).toLocaleString()}</time></p>}
      </details>}
      {overview.capabilities.recovery && (overview.authority === "act" || overview.summary.recoveryCases > 0) && <div className={styles.section} role="region" aria-label="Butler recovery">
        <h3>Autonomous recovery <span>{overview.summary.recoveryActive} active · {overview.summary.recoveryAttention} need attention</span></h3>
        {recoveryAttention.length + recoveryActive.length > 0 ? <ul className={styles.list}>
          {[...recoveryAttention, ...recoveryActive].map((item) => <RecoveryRow key={item.id} item={item} projectId={projectId} onNavigate={onNavigate} />)}
        </ul> : <p className={styles.note}>No recovery cases need attention.</p>}
        {recoverySettled.length > 0 && <details className={styles.disclosure}><summary>Past checks <span>{recoverySettled.length}</span></summary>
          <ul className={styles.list}>{recoverySettled.map((item) => <RecoveryRow key={item.id} item={item} projectId={projectId} onNavigate={onNavigate} />)}</ul>
        </details>}
        {overview.nextRecoveryOffset !== undefined && <button className="button quiet" type="button" disabled={loadingMore !== undefined || coreState !== "online"} onClick={() => void loadMore("recovery")}>{loadingMore === "recovery" ? "Loading…" : "Show more checks"}</button>}
      </div>}
      {incidentsAvailable ? <div className={styles.section} aria-label="Butler incidents">
        <h3>Needs attention <span>{overview.summary.openIncidents} open · {overview.summary.acknowledgedIncidents} acknowledged</span></h3>
        {ackError?.projectId === projectId && <p role="alert" className={styles.error}>Could not acknowledge incident. {ackError.message}</p>}
        {overview.incidents.length ? <ul className={styles.list}>{overview.incidents.map((item) => <IncidentRow key={item.id} incident={item} goals={overview.watchedGoals} projectId={projectId} onNavigate={onNavigate} onAcknowledge={(incident) => void acknowledge(incident)} acknowledging={acknowledging?.projectId === projectId && acknowledging.incidentId === item.id} canAcknowledge={canAcknowledge} />)}</ul>
          : <p className={styles.note}>{overview.summary.resolvedIncidents ? `${overview.summary.resolvedIncidents} resolved; no open incidents.` : "No incidents recorded."}</p>}
        {overview.nextIncidentOffset !== undefined && <button className="button quiet" type="button" disabled={loadingMore !== undefined || coreState !== "online"} onClick={() => void loadMore("incident")}>{loadingMore === "incident" ? "Loading…" : "Show more incidents"}</button>}
      </div> : <p className={styles.note}>Incident alerts are not active yet. Watch still tracks goal state.</p>}

      {overview.capabilities.resourceAlerts && overview.summary.accountResources + overview.summary.harnessQuotaResources > 0 && <div className={styles.section} aria-label="Resources">
        <h3>Resources <span>{overview.summary.accountResources + overview.summary.harnessQuotaResources}</span></h3>
        {overview.resources.length ? <ul className={styles.list}>{overview.resources.map((resource) => <ResourceRow key={butlerResourceKey(resource)} resource={resource} onNavigate={onNavigate} />)}</ul>
          : <p className={styles.note}>No resource observations for watched work yet.</p>}
        {overview.nextResourceOffset !== undefined && <button className="button quiet" type="button" disabled={loadingMore !== undefined || coreState !== "online"} onClick={() => void loadMore("resource")}>{loadingMore === "resource" ? "Loading…" : "Show more resources"}</button>}
      </div>}

      {overview.summary.watchedGoals > 0 ? <details className={styles.disclosure}>
        <summary>Watched goals <span>{overview.summary.watchedGoals}</span></summary>
        {overview.watchedGoals.length ? <ul className={styles.list}>{overview.watchedGoals.map((goal) => <GoalRow key={goal.goalId} goal={goal} parentGoal={overview.watchedGoals.find((candidate) => candidate.goalId === goal.parentGoalId)} projectId={projectId} onNavigate={onNavigate} />)}</ul>
          : <p className={styles.note}>Start or resume a goal to begin Watch automatically.</p>}
        {overview.nextWatchOffset !== undefined && <button className="button quiet" type="button" disabled={loadingMore !== undefined || coreState !== "online"} onClick={() => void loadMore("watch")}>{loadingMore === "watch" ? "Loading…" : "Show more goals"}</button>}
      </details> : <p className={styles.note}>Start or resume a goal to begin Watch.</p>}

      {decisionsAvailable && overview.decisions.length > 0 && <details className={styles.disclosure}>
        <summary>Recent decisions <span>{decisionsAvailable ? overview.decisions.length : "—"}</span></summary>
        {decisionsAvailable ? overview.decisions.length ? <ul className={styles.list}>{overview.decisions.map((decision) => <DecisionRow key={decision.id} decision={decision} projectId={projectId} onNavigate={onNavigate} />)}</ul>
          : <p className={styles.note}>No decisions recorded.</p>
          : <p className={styles.note}>Decision history is not active yet.</p>}
      </details>}

      {overview.capabilities.actions && overview.summary.actions > 0 && <div className={styles.section} aria-label="Butler actions">
        <h3>Recent actions <span>{overview.summary.actions}</span></h3>
        <ul className={styles.list}>{shownActions.map((action) => <ActionRow key={action.id} action={action} projectId={projectId} onNavigate={onNavigate} />)}</ul>
        {(olderActions.length > 0 || overview.nextActionOffset !== undefined) && <details className={styles.disclosure}>
          <summary>More actions</summary>
          {olderActions.length > 0 && <ul className={styles.list}>{olderActions.map((action) => <ActionRow key={action.id} action={action} projectId={projectId} onNavigate={onNavigate} />)}</ul>}
          {overview.nextActionOffset !== undefined && <button className="button quiet" type="button" disabled={loadingMore !== undefined || coreState !== "online"} onClick={() => void loadMore("action")}>{loadingMore === "action" ? "Loading…" : "Show more actions"}</button>}
        </details>}
      </div>}

      <AuthorityEditor api={api} projectId={projectId} coreState={coreState} overview={overview} onSaved={refresh} />
      <ButlerIssues api={api} projectId={projectId} online={coreState === "online"} />
    </>}
  </section>;
}
