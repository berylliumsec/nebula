import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  Check,
  ChevronLeft,
  ChevronRight,
  CirclePause,
  CirclePlay,
  Hand,
  LoaderCircle,
  Plus,
  RefreshCw,
  ShieldCheck,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { useSearchParams } from "react-router-dom";
import type { ApiClient } from "../api/client";
import { AssessmentEventStream } from "../api/assessmentEvents";
import type {
  SecurityBrowserAssessmentProfile,
  SecurityBrowserAssessmentWorkspace,
  SecurityBrowserIdentity,
  SecurityBrowserSession,
} from "../api/types";
import type { StreamState } from "../api/events";
import { logCaughtDiagnostic } from "../diagnostics";

interface SecurityBrowserWorkspacePanelProps {
  api: ApiClient;
  desktop: boolean;
  projectId: string;
  identity?: SecurityBrowserIdentity;
  session?: SecurityBrowserSession;
  targetOptions: string[];
  toolNavigation: ReactNode;
  children: ReactNode;
  onClose: () => void;
}

const PROFILE_ORDER: SecurityBrowserAssessmentProfile[] = [
  "explore",
  "standard",
  "deep",
  "api",
  "validation",
];

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function stateLabel(value: string): string {
  return value.replaceAll("_", " ");
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function SecurityBrowserWorkspacePanel({
  api,
  desktop,
  projectId,
  identity,
  session,
  targetOptions,
  toolNavigation,
  children,
  onClose,
}: SecurityBrowserWorkspacePanelProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [workspace, setWorkspace] = useState<SecurityBrowserAssessmentWorkspace>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [streamState, setStreamState] = useState<StreamState>("closed");
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardStep, setWizardStep] = useState(0);
  const [target, setTarget] = useState("");
  const [objective, setObjective] = useState("");
  const [profileId, setProfileId] = useState<SecurityBrowserAssessmentProfile>("standard");
  const [validationTechnique, setValidationTechnique] = useState("");
  const [validationMaxRequests, setValidationMaxRequests] = useState(10);
  const [validationDurationSeconds, setValidationDurationSeconds] = useState(600);

  const selectedId = searchParams.get("assessment") ?? undefined;
  const selected = workspace?.assessments.find((assessment) => assessment.id === selectedId);
  const selectedCandidateId = searchParams.get("candidate") ?? undefined;

  const selectAssessment = useCallback((assessmentId?: string) => {
    const next = new URLSearchParams(searchParams);
    if (assessmentId) next.set("assessment", assessmentId);
    else next.delete("assessment");
    next.delete("candidate");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const selectCandidate = useCallback((candidateId?: string) => {
    const next = new URLSearchParams(searchParams);
    if (candidateId) next.set("candidate", candidateId);
    else next.delete("candidate");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await api.getSecurityBrowserAssessments(projectId, signal);
      setWorkspace(next);
      setError(undefined);
      if (selectedId && !next.assessments.some((item) => item.id === selectedId)) {
        selectAssessment(next.assessments.at(-1)?.id);
      } else if (!selectedId && next.assessments.length) {
        selectAssessment(next.assessments.at(-1)?.id);
      }
    } catch (caught) {
      if (signal?.aborted) return;
      void logCaughtDiagnostic(
        "interface.security_browser.assessment_snapshot_failed",
        "The Security Browser assessment snapshot could not be loaded.",
        caught,
        "security_browser",
      );
      setError(`${message(caught)} Manual browsing remains available; retry the assessment snapshot.`);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, projectId, selectAssessment, selectedId]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (!selectedId) {
      setStreamState("closed");
      return;
    }
    const stream = new AssessmentEventStream({
      apiBaseUrl: api.baseUrl,
      token: api.getToken(),
      assessmentId: selectedId,
      onStateChange: setStreamState,
      onEvent: () => void refresh(),
    });
    stream.connect();
    return () => stream.disconnect();
  }, [api, refresh, selectedId]);

  useEffect(() => {
    if (!target && targetOptions[0]) setTarget(targetOptions[0]);
  }, [target, targetOptions]);

  const profile = workspace?.profiles.find((item) => item.id === profileId);
  const selectedSteps = useMemo(
    () => workspace?.steps.filter((step) => step.assessmentId === selected?.id)
      .sort((left, right) => left.sequence - right.sequence) ?? [],
    [selected?.id, workspace?.steps],
  );
  const selectedCandidates = workspace?.candidates.filter(
    (candidate) => candidate.assessmentId === selected?.id,
  ) ?? [];
  const selectedCandidate = selectedCandidates.find(
    (candidate) => candidate.id === selectedCandidateId,
  );
  const selectedValidationGrant = workspace?.validationGrants.find(
    (grant) => grant.id === selectedCandidate?.validationGrantId,
  );

  useEffect(() => {
    if (selectedCandidateId && !selectedCandidate) selectCandidate(undefined);
  }, [selectCandidate, selectedCandidate, selectedCandidateId]);

  useEffect(() => {
    setValidationTechnique("");
    setValidationMaxRequests(10);
    setValidationDurationSeconds(600);
  }, [selectedCandidate?.id]);

  const openWizard = () => {
    setWizardStep(0);
    setTarget((current) => current || targetOptions[0] || "");
    setObjective((current) => current || `Assess ${targetOptions[0] ?? "the selected target"} and preserve reviewable evidence.`);
    setProfileId("standard");
    setWizardOpen(true);
  };

  const createAssessment = async () => {
    if (!identity || !session || !target || !objective.trim() || !profile) return;
    setBusy(true);
    setError(undefined);
    try {
      const created = await api.createSecurityBrowserAssessment(projectId, {
        name: `${profile.name} · ${new URL(target).hostname}`,
        objective: objective.trim(),
        profile: profile.id,
        sessionId: session.id,
        identityIds: [identity.id],
        primaryIdentityId: identity.id,
        targetUrls: [target],
        budget: profile.defaultBudget,
      });
      setWizardOpen(false);
      selectAssessment(created.id);
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic(
        "interface.security_browser.assessment_create_failed",
        "The guided Security Browser assessment could not be created.",
        caught,
        "security_browser",
      );
      setError(`${message(caught)} No assessment or browser execution was started.`);
    } finally {
      setBusy(false);
    }
  };

  const transition = async (
    action: Parameters<ApiClient["transitionSecurityBrowserAssessment"]>[1],
    options?: { reason?: string; recoveryAction?: string },
  ) => {
    if (!selected) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.transitionSecurityBrowserAssessment(selected, action, options);
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic(
        "interface.security_browser.assessment_transition_failed",
        `The Security Browser assessment could not ${action}.`,
        caught,
        "security_browser",
      );
      setError(`${message(caught)} The live browser remains usable; refresh readiness or retry the valid action.`);
    } finally {
      setBusy(false);
    }
  };

  const refreshReadiness = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await api.refreshSecurityBrowserAssessmentReadiness(selected);
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic(
        "interface.security_browser.assessment_readiness_failed",
        "The Security Browser assessment readiness could not be refreshed.",
        caught,
        "security_browser",
      );
      setError(`${message(caught)} Manual legacy browsing remains available.`);
    } finally {
      setBusy(false);
    }
  };

  const deleteSelected = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await api.deleteSecurityBrowserAssessment(selected);
      selectAssessment(undefined);
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic(
        "interface.security_browser.assessment_delete_failed",
        "The Security Browser assessment could not be deleted.",
        caught,
        "security_browser",
      );
      setError(`${message(caught)} Stop or revoke the assessment, then retry deletion.`);
    } finally {
      setBusy(false);
    }
  };

  const grantValidation = async () => {
    if (!selectedCandidate || !validationTechnique.trim()) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.grantSecurityBrowserCandidateValidation(selectedCandidate, {
        technique: validationTechnique.trim(),
        maxRequests: validationMaxRequests,
        durationSeconds: validationDurationSeconds,
      });
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic(
        "interface.security_browser.validation_grant_failed",
        "The Security Browser validation grant could not be created.",
        caught,
        "security_browser",
      );
      setError(`${message(caught)} The issue remains an unvalidated candidate; review its target-specific exploitation grant and retry from this detail.`);
    } finally {
      setBusy(false);
    }
  };

  const revokeValidation = async () => {
    if (!selectedCandidate || !selectedValidationGrant) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.revokeSecurityBrowserCandidateValidation(
        selectedCandidate,
        selectedValidationGrant,
        "Emergency revocation requested by the operator from issue detail.",
      );
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic(
        "interface.security_browser.validation_revoke_failed",
        "The Security Browser validation grant could not be revoked.",
        caught,
        "security_browser",
      );
      setError(`${message(caught)} Validation remains visibly bounded by its existing expiry; stop the assessment for the broader emergency kill switch.`);
    } finally {
      setBusy(false);
    }
  };

  const wizardPages = [
    <section key="target" className="security-browser-wizard-page">
      <span className="security-browser-wizard-kicker">1 · Target</span>
      <h3>Where may Nebula test?</h3>
      <p>Choose an enumerated Project target. The saved assessment freezes the current scope revision.</p>
      <label>Authorized target<select value={target} onChange={(event) => setTarget(event.target.value)}><option value="">Choose a target</option>{targetOptions.map((option) => <option key={option} value={option}>{option}</option>)}</select></label>
      {!targetOptions.length && <div className="security-browser-callout warning" role="alert"><AlertTriangle size={16} /> No HTTP target is available. Add a URL or domain to Project scope, then return here.</div>}
    </section>,
    <section key="identity" className="security-browser-wizard-page">
      <span className="security-browser-wizard-kicker">2 · Identity and login</span>
      <h3>Use one continuous browser identity</h3>
      <p>Manual takeover and autonomous work share this identity, cookies, traffic history, and evidence chain.</p>
      <dl><div><dt>Identity</dt><dd>{identity?.name ?? "No identity selected"}</dd></div><div><dt>Session</dt><dd>{session?.name ?? "No session selected"}</dd></div><div><dt>Login handling</dt><dd>Take over for login, MFA, CAPTCHA, consent, or ambiguity.</dd></div></dl>
    </section>,
    <section key="objective" className="security-browser-wizard-page">
      <span className="security-browser-wizard-kicker">3 · Objective</span>
      <h3>What outcome should the test pursue?</h3>
      <p>Keep the goal concrete. Nebula will show a reviewable staged plan before execution.</p>
      <label>Assessment goal<textarea rows={5} maxLength={4000} value={objective} onChange={(event) => setObjective(event.target.value)} /></label>
    </section>,
    <section key="profile" className="security-browser-wizard-page">
      <span className="security-browser-wizard-kicker">4 · Test profile</span>
      <h3>Choose depth and traffic risk</h3>
      <div className="security-browser-profile-grid">{PROFILE_ORDER.map((id) => {
        const option = workspace?.profiles.find((item) => item.id === id);
        if (!option) return null;
        const locked = option.validationLocked;
        return <button key={id} type="button" className={profileId === id ? "selected" : ""} disabled={locked} onClick={() => setProfileId(id)} aria-pressed={profileId === id}><strong>{option.name}</strong><span>{option.summary}</span>{locked && <small>Start from an issue and request a bounded validation grant.</small>}</button>;
      })}</div>
    </section>,
    <section key="review" className="security-browser-wizard-page">
      <span className="security-browser-wizard-kicker">5 · Scope, risk, and budget</span>
      <h3>Review before anything executes</h3>
      <dl><div><dt>Target</dt><dd>{target || "Not selected"}</dd></div><div><dt>Identity</dt><dd>{identity?.name ?? "Unavailable"}</dd></div><div><dt>Profile</dt><dd>{profile?.name ?? "Unavailable"}</dd></div><div><dt>Risk</dt><dd>{profile?.riskClasses.join(", ") || "None"}</dd></div><div><dt>Request budget</dt><dd>{profile?.defaultBudget.maxRequests ?? 0}</dd></div><div><dt>Duration</dt><dd>{Math.round((profile?.defaultBudget.maxDurationSeconds ?? 0) / 60)} minutes</dd></div></dl>
      <div className="security-browser-engine-review">{profile?.requiredAdapters.map((adapter) => {
        const engine = workspace?.engines.find((item) => item.adapter === adapter);
        return <div key={adapter} className={`engine-${engine?.state ?? "unavailable"}`}><span>{engine?.state === "ready" ? <Check size={14} /> : <AlertTriangle size={14} />}</span><div><strong>{engine?.displayName ?? adapter}</strong><small>{engine?.state === "ready" ? `${engine.installedVersion ?? "Installed"} · contract v${engine.contractVersion}` : engine?.unavailabilityReason ?? "Runtime status unavailable"}</small></div></div>;
      })}</div>
      <p>Creating the assessment stores preflight and plan state. Execution starts separately and remains blocked until every required adapter reports ready.</p>
    </section>,
  ];

  return <aside className="browser-research-panel security-browser-workspace" aria-label="Security Browser workspace">
    <header>
      <div><strong>Security Browser</strong><small>Guided assessments, live browser, evidence, and expert tools</small></div>
      <div className="security-browser-header-actions"><span className={`security-browser-stream stream-${streamState}`}>{stateLabel(streamState)}</span><button type="button" aria-label="Close Security Browser" onClick={onClose}><X size={17} /></button></div>
    </header>
    {error && <div className="security-browser-error" role="alert"><AlertTriangle size={16} /><span>{error}</span><button type="button" onClick={() => void refresh()}><RefreshCw size={14} /> Retry</button></div>}
    <div className="security-browser-layout">
      <aside className="security-browser-assessment-rail" aria-label="Assessments">
        <button className="button primary" type="button" disabled={!desktop || !identity || !session} onClick={openWizard}><Plus size={15} /> Start guided test</button>
        {!desktop && <p>Monitor, approve, pause, stop, and retry here. Start and live traffic editing stay on the paired desktop.</p>}
        <div className="security-browser-assessment-list">
          {loading ? <span className="security-browser-loading"><LoaderCircle className="spin" size={15} /> Loading assessments…</span> : workspace?.assessments.length ? [...workspace.assessments].reverse().map((assessment) => <button key={assessment.id} type="button" className={assessment.id === selected?.id ? "selected" : ""} onClick={() => selectAssessment(assessment.id)}><span className={`security-browser-status status-${assessment.status}`}>{stateLabel(assessment.status)}</span><strong>{assessment.name}</strong><small>{assessment.targetUrls[0]}</small><span>{percent(assessment.progress)} · {stateLabel(assessment.phase)}</span></button>) : <div className="security-browser-empty"><ShieldCheck size={20} /><strong>No assessments yet</strong><span>Start guided test to freeze scope, identity, profile, and budget.</span></div>}
        </div>
      </aside>
      <section className="security-browser-main">
        {selected ? <div className="security-browser-run-summary">
          <header><div><span className={`security-browser-status status-${selected.status}`}>{stateLabel(selected.status)}</span><h2>{selected.name}</h2><p>{selected.objective}</p></div><div className="security-browser-run-controls">
            {selected.status === "ready" && <button className="button primary" type="button" disabled={!desktop || busy} onClick={() => void transition("start")}><CirclePlay size={15} /> Start</button>}
            {selected.status === "running" && <><button className="button secondary" type="button" disabled={busy} onClick={() => void transition("takeover", { reason: "Operator requested control of the live browser." })}><Hand size={15} /> Take over</button><button className="button secondary" type="button" disabled={busy} onClick={() => void transition("pause", { reason: "Paused by operator." })}><CirclePause size={15} /> Pause</button><button className="button danger" type="button" disabled={busy} onClick={() => void transition("stop")}><Square size={14} /> Stop</button></>}
            {selected.status === "waiting_operator" && <button className="button primary" type="button" disabled={!desktop || busy} onClick={() => void transition("return_control")}><CirclePlay size={15} /> Return control to Nebula</button>}
            {selected.status === "paused" && <><button className="button primary" type="button" disabled={!desktop || busy} onClick={() => void transition("resume")}><CirclePlay size={15} /> Resume</button><button className="button danger" type="button" disabled={busy} onClick={() => void transition("stop")}><Square size={14} /> Stop</button></>}
            {selected.status === "draft" && <button className="button secondary" type="button" disabled={busy} onClick={() => void refreshReadiness()}><RefreshCw size={14} /> Prepare / Retry</button>}
            {["failed", "stopped"].includes(selected.status) && <button className="button secondary" type="button" disabled={busy} onClick={() => void transition("retry")}><RefreshCw size={14} /> Retry preflight</button>}
          </div></header>
          {(selected.pauseReason || selected.failure) && <div className="security-browser-callout warning"><AlertTriangle size={16} /><div><strong>{selected.failure ? "Assessment failed" : "Action required"}</strong><span>{selected.failure ?? selected.pauseReason}</span><small>{selected.recoveryAction ?? "The live browser and saved evidence remain usable."}</small></div></div>}
          <div className="security-browser-metrics"><div><span>Phase</span><strong>{stateLabel(selected.phase)}</strong></div><div><span>Coverage</span><strong>{selected.coverage.visitedUrls} / {selected.coverage.discoveredUrls} URLs</strong></div><div><span>Requests</span><strong>{selected.budget.requestsUsed} / {selected.budget.maxRequests}</strong></div><div><span>Candidates</span><strong>{selectedCandidates.length}</strong></div><div><span>Control</span><strong>{selected.controlOwner}</strong></div></div>
          <div className="security-browser-progress" aria-label={`${percent(selected.progress)} complete`}><span style={{ width: percent(selected.progress) }} /></div>
          <details><summary>Proposed plan · {selectedSteps.length} steps</summary><ol>{selectedSteps.map((step) => <li key={step.id}><span className={`security-browser-step step-${step.status}`}>{stateLabel(step.status)}</span><div><strong>{step.title}</strong><small>{step.intent}</small>{step.error && <em>{step.error} {step.recoveryAction}</em>}</div></li>)}</ol></details>
          {selectedCandidates.length > 0 && <section className="security-browser-candidates" aria-labelledby="security-browser-candidates-title">
            <header><div><h3 id="security-browser-candidates-title">Candidate issues</h3><p>Candidate status is separate from Findings. Validation requires evidence and never promotes automatically.</p></div><span>{selectedCandidates.length}</span></header>
            <div className="security-browser-candidate-layout">
              <div className="security-browser-candidate-list">{selectedCandidates.map((candidate) => <button key={candidate.id} type="button" className={candidate.id === selectedCandidate?.id ? "selected" : ""} onClick={() => selectCandidate(candidate.id)} aria-pressed={candidate.id === selectedCandidate?.id}><span className={`security-browser-status status-${candidate.validationStatus}`}>{stateLabel(candidate.validationStatus)}</span><strong>{candidate.title}</strong><small>{candidate.severity} · {candidate.confidence}</small></button>)}</div>
              {selectedCandidate && <article className="security-browser-candidate-detail">
                <header><div><span className={`security-browser-status status-${selectedCandidate.validationStatus}`}>{stateLabel(selectedCandidate.validationStatus)}</span><h4>{selectedCandidate.title}</h4></div><button type="button" aria-label="Close candidate detail" onClick={() => selectCandidate(undefined)}><X size={15} /></button></header>
                <dl><div><dt>Target</dt><dd><code>{selectedCandidate.targetUrl}</code></dd></div><div><dt>Insertion point</dt><dd>{selectedCandidate.insertionPoint ?? "Whole request"}</dd></div><div><dt>Check</dt><dd>{selectedCandidate.ruleId}{selectedCandidate.cwe ? ` · ${selectedCandidate.cwe}` : ""}</dd></div><div><dt>Evidence</dt><dd>{selectedCandidate.evidenceIds.length} retained item{selectedCandidate.evidenceIds.length === 1 ? "" : "s"}</dd></div></dl>
                {selectedValidationGrant ? <div className="security-browser-grant-status"><div className="security-browser-callout warning" role="status"><ShieldCheck size={16} /><div><strong>{selectedValidationGrant.status === "active" && new Date(selectedValidationGrant.expiresAt).getTime() > Date.now() ? "Bounded validation authorized" : `Validation grant ${selectedValidationGrant.status === "active" ? "expired" : selectedValidationGrant.status}`}</strong><span>{selectedValidationGrant.technique}</span><small>Up to {selectedValidationGrant.maxRequests} requests over {selectedValidationGrant.durationSeconds} seconds to this target only; expires {new Date(selectedValidationGrant.expiresAt).toLocaleString()}.</small></div></div>{selectedValidationGrant.status === "active" && new Date(selectedValidationGrant.expiresAt).getTime() > Date.now() && <button className="button danger" type="button" disabled={busy} onClick={() => void revokeValidation()}><Square size={14} /> Revoke validation now</button>}</div> : <div className="security-browser-validation-form">
                  <div className="security-browser-callout warning" role="alert"><AlertTriangle size={16} /><div><strong>This authorizes exploit-validation traffic</strong><span>Name one exact technique. Nebula will still enforce the frozen target, duration, request budget, and emergency revocation independently.</span></div></div>
                  <label>Exact validation technique<textarea rows={3} maxLength={1000} value={validationTechnique} onChange={(event) => setValidationTechnique(event.target.value)} placeholder="Describe the probe and its negative control without placing secrets here." /></label>
                  <div className="security-browser-validation-budget"><label>Maximum requests<input type="number" min={1} max={10000} value={validationMaxRequests} onChange={(event) => setValidationMaxRequests(Number(event.target.value))} /></label><label>Duration (seconds)<input type="number" min={30} max={3600} value={validationDurationSeconds} onChange={(event) => setValidationDurationSeconds(Number(event.target.value))} /></label></div>
                  <div className="security-browser-traffic-preview"><strong>Expected traffic</strong><span>Up to {validationMaxRequests} requests over {validationDurationSeconds} seconds to <code>{selectedCandidate.targetUrl}</code> through the policy path.</span></div>
                  <button className="button danger" type="button" disabled={busy || !validationTechnique.trim() || validationMaxRequests < 1 || validationMaxRequests > 10000 || validationDurationSeconds < 30 || validationDurationSeconds > 3600} onClick={() => void grantValidation()}><ShieldCheck size={15} /> Grant bounded validation</button>
                </div>}
              </article>}
            </div>
          </section>}
          {selected.status !== "running" && <button className="security-browser-delete" type="button" disabled={busy} onClick={() => void deleteSelected()}><Trash2 size={13} /> Delete assessment metadata</button>}
        </div> : <div className="security-browser-welcome"><ShieldCheck size={28} /><h2>One workspace for manual and autonomous testing</h2><p>Select an assessment or start a guided test. Browser identity, traffic, frozen scope, evidence, and candidate review remain connected.</p></div>}
        <div className="security-browser-tool-dock">
          {toolNavigation}
          <div className="security-browser-tool-content">{children}</div>
        </div>
      </section>
    </div>
    {wizardOpen && <div className="security-browser-wizard-backdrop" role="presentation">
      <div className="security-browser-wizard" role="dialog" aria-modal="true" aria-labelledby="security-browser-wizard-title">
        <header><div><span>Guided test preflight</span><strong id="security-browser-wizard-title">{["Target", "Identity and login", "Objective", "Test profile", "Final review"][wizardStep]}</strong></div><button type="button" aria-label="Close guided test" onClick={() => setWizardOpen(false)}><X size={17} /></button></header>
        <div className="security-browser-wizard-steps" aria-label={`Step ${wizardStep + 1} of 5`}>{[0, 1, 2, 3, 4].map((step) => <span key={step} className={step <= wizardStep ? "active" : ""}>{step + 1}</span>)}</div>
        {wizardPages[wizardStep]}
        <footer><button className="button secondary" type="button" disabled={wizardStep === 0 || busy} onClick={() => setWizardStep((step) => step - 1)}><ChevronLeft size={15} /> Back</button>{wizardStep < 4 ? <button className="button primary" type="button" disabled={(wizardStep === 0 && !target) || (wizardStep === 1 && (!identity || !session)) || (wizardStep === 2 && !objective.trim())} onClick={() => setWizardStep((step) => step + 1)}>Continue <ChevronRight size={15} /></button> : <button className="button primary" type="button" disabled={busy || !target || !identity || !session || !profile} onClick={() => void createAssessment()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />} Create assessment</button>}</footer>
      </div>
    </div>}
  </aside>;
}
