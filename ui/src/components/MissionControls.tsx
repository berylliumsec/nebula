import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Play, ShieldCheck, Square, Trash2, Wrench, X } from "lucide-react";
import { providerModelVerification } from "../api/providerCapabilities";
import type { HarnessProfile, HarnessSessionSummary, McpServerProfile } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { useConfirmation } from "./DialogSystem";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

interface NewMissionButtonProps {
  className?: string;
  children?: ReactNode;
}

export function NewMissionButton({ className = "button primary", children }: NewMissionButtonProps) {
  const confirm = useConfirmation();
  const { api, coreState, engagement, previewMode, providers, reverifyProvider, startMission } = useWorkspace();
  const availableProviders = useMemo(() => providers.filter((provider) => provider.enabled), [providers]);
  const [runtimeKind, setRuntimeKind] = useState<"native" | "harness">("native");
  const [harnesses, setHarnesses] = useState<HarnessProfile[]>([]);
  const [harnessSessions, setHarnessSessions] = useState<HarnessSessionSummary[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServerProfile[]>([]);
  const [harnessId, setHarnessId] = useState("");
  const [harnessSessionId, setHarnessSessionId] = useState("");
  const [selectedMcpIds, setSelectedMcpIds] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [objective, setObjective] = useState("");
  const [providerId, setProviderId] = useState("");
  const provider = availableProviders.find((item) => item.id === providerId);
  const [model, setModel] = useState("");
  const [durationMinutes, setDurationMinutes] = useState(60);
  const [maxTokens, setMaxTokens] = useState(20_000);
  const [maxCost, setMaxCost] = useState(10);
  const [maxRetries, setMaxRetries] = useState(1);
  const [runtimeReady, setRuntimeReady] = useState(false);
  const [runtimeConfigured, setRuntimeConfigured] = useState(false);
  const [maxToolCalls, setMaxToolCalls] = useState(0);
  const [maxConcurrency, setMaxConcurrency] = useState(1);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [toolPreparation, setToolPreparation] = useState<"idle" | "preparing" | "ready" | "unavailable">("idle");
  const [toolPreparationDetail, setToolPreparationDetail] = useState<string>();
  const [toolVerificationBusy, setToolVerificationBusy] = useState(false);
  const attemptedToolVerificationRef = useRef(new Set<string>());
  const selectedHarness = harnesses.find((item) => item.id === harnessId);
  const attachedHarnessSession = harnessSessions.find((item) => item.id === harnessSessionId);
  const modelOptions = [...new Set([
    ...(runtimeKind === "native"
      ? provider?.models ?? []
      : attachedHarnessSession
        ? [attachedHarnessSession.model]
        : selectedHarness?.models ?? []),
    ...(model ? [model] : []),
  ])];

  useEffect(() => {
    if (availableProviders.some((item) => item.id === providerId)) return;
    const next = availableProviders[0];
    setProviderId(next?.id ?? "");
    setModel(next?.defaultModel ?? next?.models[0] ?? "");
  }, [availableProviders, providerId]);

  useEffect(() => {
    let active = true;
    if (!api || coreState !== "online") return () => { active = false; };
    void Promise.all([api.listHarnesses(), api.listMcpServers(), api.listHarnessSessions(engagement?.id)])
      .then(([nextHarnesses, nextServers, nextSessions]) => {
        if (!active) return;
        const enabled = nextHarnesses.filter((item) => item.enabled);
        setHarnesses(enabled);
        setMcpServers(nextServers.filter((item) => item.enabled));
        setHarnessSessions(nextSessions.filter((item) => item.status !== "closed"));
        setHarnessId((current) => enabled.some((item) => item.id === current) ? current : enabled[0]?.id ?? "");
      })
      .catch((caughtError) => {
        void logCaughtDiagnostic("interface.mission_controls.caught_failure_01", "A handled interface operation failed.", caughtError, "mission_controls"); if (active) { setHarnesses([]); setMcpServers([]); setHarnessSessions([]); } });
    return () => { active = false; };
  }, [api, coreState, engagement?.id]);

  useEffect(() => {
    if (runtimeKind !== "harness") return;
    const attached = harnessSessions.find((item) => item.id === harnessSessionId);
    const harness = harnesses.find((item) => item.id === (attached?.harnessProfileId ?? harnessId));
    if (attached) setHarnessId(attached.harnessProfileId);
    setModel(attached?.model ?? harness?.defaultModel ?? harness?.models[0] ?? "");
  }, [harnessId, harnessSessionId, harnessSessions, harnesses, runtimeKind]);

  useEffect(() => {
    let active = true;
    if (!api || coreState !== "online") {
      setRuntimeReady(false);
      setRuntimeConfigured(false);
      return () => { active = false; };
    }
    setToolPreparation("preparing");
    void api.getAutomationRuntime()
      .then((runtime) => {
        if (!active) return;
        setRuntimeReady(runtime.ready);
        setRuntimeConfigured(runtime.configured);
        setToolPreparation(runtime.ready ? "ready" : "unavailable");
        setToolPreparationDetail(runtime.ready ? undefined : runtime.detail);
      })
      .catch((caughtError) => {
        void logCaughtDiagnostic("interface.mission_controls.caught_failure_02", "A handled interface operation failed.", caughtError, "mission_controls");
        if (!active) return;
        setRuntimeReady(false);
        setRuntimeConfigured(false);
        setToolPreparation("unavailable");
        setToolPreparationDetail(caughtError instanceof Error ? caughtError.message : "Command runtime is unavailable.");
      });
    return () => { active = false; };
  }, [api, coreState]);

  const verification = providerModelVerification(provider, model);
  const providerSupportsTools = verification?.status === "verified";
  const automaticTools = useMemo(() => providerSupportsTools && runtimeReady
    ? ["run_command", "process_io"]
    : [], [providerSupportsTools, runtimeReady]);
  const toolSelectionMessage = toolVerificationBusy
    ? `Checking tool support for ${model.trim()}…`
    : toolPreparation === "preparing"
    ? toolPreparationDetail ?? "Checking the command runtime…"
    : !runtimeConfigured
    ? coreState !== "online"
      ? "Nebula Core is offline; reconnect Core before using command execution."
      : "The pinned automation runtime is not configured."
    : !providerSupportsTools
      ? verification?.status === "failed"
        ? `Tool verification failed for ${model}: ${verification.failureDetail ?? "the provider did not return a valid structured call"}. Reverify it in Settings.`
        : model
          ? `Tool calling has not been verified for ${model}. Verify it in Settings.`
          : "Select a model and verify tool calling in Settings."
      : !runtimeReady
        ? toolPreparationDetail ?? "Prepare the pinned automation runtime in Settings."
        : undefined;

  useEffect(() => {
    if (!open || coreState !== "online" || previewMode || !provider || !model.trim() || verification) return;
    const key = `${provider.id}:${model.trim()}`;
    if (attemptedToolVerificationRef.current.has(key)) return;
    attemptedToolVerificationRef.current.add(key);
    let active = true;
    setToolVerificationBusy(true);
    void reverifyProvider(provider.id, model)
      .catch((caughtError) => { void logCaughtDiagnostic("interface.mission_controls.caught_failure_03", "A handled interface operation failed.", caughtError, "mission_controls"); return undefined; })
      .finally(() => { if (active) setToolVerificationBusy(false); });
    return () => { active = false; };
  }, [coreState, model, open, previewMode, provider, reverifyProvider, verification]);

  const selectProvider = (id: string) => {
    const next = availableProviders.find((item) => item.id === id);
    setProviderId(id);
    setModel(next?.defaultModel ?? next?.models[0] ?? "");
  };

  const openMission = () => {
    setError(undefined);
    setMaxToolCalls(runtimeKind === "harness" || automaticTools.length || selectedMcpIds.length ? 50 : 0);
    setMaxConcurrency(automaticTools.length ? 2 : 1);
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    setMaxToolCalls(runtimeKind === "harness" || automaticTools.length || selectedMcpIds.length ? 50 : 0);
    setMaxConcurrency(runtimeKind === "native" && (automaticTools.length || selectedMcpIds.length) ? 2 : 1);
  }, [automaticTools, open, runtimeKind, selectedMcpIds.length]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const cleanName = name.trim();
    const cleanObjective = objective.trim();
    const cleanModel = model.trim();
    if (!engagement) {
      setError("Select an engagement before starting a mission.");
      return;
    }
    if (runtimeKind === "native" && !provider) {
      setError("Select an enabled provider before starting a mission.");
      return;
    }
    if (runtimeKind === "harness" && !selectedHarness) {
      setError("Select an enabled agent harness before starting a mission.");
      return;
    }
    if (!cleanName) {
      setError("Enter a mission name so you can identify it later.");
      return;
    }
    if (!cleanObjective) {
      setError("Enter a mission objective.");
      return;
    }
    if (!cleanModel) {
      setError("Select a model for this mission.");
      return;
    }
    if (!Number.isInteger(durationMinutes) || durationMinutes < 1 || durationMinutes > 60) {
      setError("Duration must be a whole number from 1 to 60 minutes.");
      return;
    }
    if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 200_000) {
      setError("Token limit must be a whole number from 1 to 200,000.");
      return;
    }
    if (!Number.isFinite(maxCost) || maxCost < 0 || maxCost > 100) {
      setError("Cost limit must be from $0 to $100.");
      return;
    }
    if (!Number.isInteger(maxRetries) || maxRetries < 0 || maxRetries > 2) {
      setError("Retries must be a whole number from 0 to 2.");
      return;
    }
    if ((runtimeKind === "harness" || automaticTools.length || selectedMcpIds.length) && (!Number.isInteger(maxToolCalls) || maxToolCalls < 1 || maxToolCalls > 100)) {
      setError("Maximum tool calls must be a whole number from 1 to 100.");
      return;
    }
    if ((automaticTools.length || selectedMcpIds.length) && (!Number.isInteger(maxConcurrency) || maxConcurrency < 1 || maxConcurrency > 2)) {
      setError("Maximum concurrency must be 1 or 2.");
      return;
    }
    const selectedHarnessSession = harnessSessions.find((item) => item.id === harnessSessionId);
    const runtimeUsesMcp = runtimeKind === "harness"
      ? Boolean(harnessSessionId ? selectedHarnessSession?.mcpServerIds.length : selectedMcpIds.length)
      : selectedMcpIds.length > 0;
    if (runtimeKind === "native" && runtimeUsesMcp && !providerSupportsTools) {
      setError(`Tool calling must be verified for ${cleanModel} before selecting MCP tools.`);
      return;
    }
    let allowCloudToolResults = false;
    const selectedRuntime = runtimeKind === "harness" ? selectedHarness : provider;
    const runtimeIsLocal = runtimeKind === "harness"
      ? selectedHarness?.localOnly === true
      : provider?.kind === "local" || provider?.privacy === "local_only";
    const runtimePermitsSensitive = runtimeKind === "harness"
      ? selectedHarness?.permitsSensitiveData
      : provider?.permitsSensitiveData;
    if (runtimeUsesMcp && selectedRuntime && !runtimeIsLocal) {
      if (!runtimePermitsSensitive) {
        setError("This runtime profile is text-only. Permit project/document data in Settings or remove MCP servers.");
        return;
      }
      allowCloudToolResults = await confirm({
        title: "Allow MCP results in this mission?",
        message: `Allow bounded MCP tool inputs and result excerpts to reach ${selectedRuntime.name} for this mission? Raw artifacts remain local and every risky call follows its approval policy.`,
        confirmLabel: "Allow this mission",
      });
      if (!allowCloudToolResults) return;
    }
    setSaving(true);
    setError(undefined);
    try {
      await startMission(runtimeKind === "harness" ? {
        engagementId: engagement.id,
        name: cleanName,
        objective: cleanObjective,
        backend: "harness",
        harnessProfileId: selectedHarness?.id,
        harnessSessionId: harnessSessionId || undefined,
        mcpServerIds: harnessSessionId ? [] : selectedMcpIds,
        model: cleanModel,
        maxDurationSeconds: durationMinutes * 60,
        maxTokens,
        maxCostUsd: maxCost,
        maxRetries: 0,
        maxToolCalls,
        maxConcurrency: 1,
        allowCloudToolResults,
      } : { engagementId: engagement.id, name: cleanName, objective: cleanObjective, backend: "native", providerId: provider?.id, mcpServerIds: selectedMcpIds, model: cleanModel, maxDurationSeconds: durationMinutes * 60, maxTokens, maxCostUsd: maxCost, maxRetries, maxToolCalls: automaticTools.length || selectedMcpIds.length ? maxToolCalls : 0, maxConcurrency: automaticTools.length || selectedMcpIds.length ? maxConcurrency : 1, allowCloudToolResults });
      setOpen(false);
      setName("");
      setObjective("");
      setMaxToolCalls(0);
      setMaxConcurrency(1);
    } catch (startError) {
      void logCaughtDiagnostic("interface.mission_controls.caught_failure_05", "A handled interface operation failed.", startError, "mission_controls");
      setError(startError instanceof Error ? startError.message : "Could not start the mission.");
    } finally {
      setSaving(false);
    }
  };

  return <>
    <button className={className} type="button" disabled={previewMode || !engagement || (availableProviders.length === 0 && harnesses.length === 0)} title={availableProviders.length || harnesses.length ? undefined : "Add an enabled provider or agent harness before automating a task"} onClick={openMission}>{children ?? <><Play size={16} /> Automate task</>}</button>
    {open && createPortal(
      <div className="dialog-backdrop">
        <form noValidate className="provider-dialog resource-dialog mission-dialog" role="dialog" aria-modal="true" aria-labelledby="mission-dialog-title" onSubmit={(event) => void submit(event)}>
          <header>
            <div><small>{automaticTools.length || selectedMcpIds.length ? "Supervised automation" : "Analysis-only automation"}</small><h2 id="mission-dialog-title">Automate task</h2></div>
            <button className="icon-button subtle" type="button" aria-label="Close automation dialog" onClick={() => setOpen(false)}><X size={17} /></button>
          </header>
          <label>Mission name<input required autoFocus maxLength={300} value={name} placeholder="Quarterly perimeter review" onChange={(event) => { setName(event.target.value); setError(undefined); }} /></label>
          <label>Objective<textarea required rows={5} value={objective} placeholder="Describe the outcome you want Nebula to produce…" onChange={(event) => { setObjective(event.target.value); setError(undefined); }} /></label>
          <details className="provider-advanced mission-advanced">
            <summary>Advanced</summary>
            <label>Runtime<select aria-label="Mission runtime" value={runtimeKind} onChange={(event) => { const next = event.target.value as "native" | "harness"; setRuntimeKind(next); setHarnessSessionId(""); setSelectedMcpIds([]); if (next === "native") selectProvider(providerId || availableProviders[0]?.id || ""); }}><option value="native">Native mission</option><option value="harness">Agent harness</option></select></label>
            {runtimeKind === "native" ? <label>Provider<select value={providerId} onChange={(event) => { selectProvider(event.target.value); setError(undefined); }}>{availableProviders.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label> : <><label>Harness<select aria-label="Mission harness" value={harnessId} disabled={Boolean(harnessSessionId)} onChange={(event) => { setHarnessId(event.target.value); setError(undefined); }}>{harnesses.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>Session<select aria-label="Harness session" value={harnessSessionId} onChange={(event) => setHarnessSessionId(event.target.value)}><option value="">Start a new session</option>{harnessSessions.filter((item) => item.harnessProfileId === harnessId || item.id === harnessSessionId).map((item) => <option value={item.id} key={item.id}>{item.model} · {item.status}</option>)}</select></label></>}
            <label>Model<select required value={model} disabled={Boolean(harnessSessionId) || !modelOptions.length} onChange={(event) => { setModel(event.target.value); setError(undefined); }}><option value="">{modelOptions.length ? "Select model" : runtimeKind === "harness" ? "Run a harness check to discover models" : "Run provider health check to discover models"}</option>{modelOptions.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
            {(runtimeKind === "native" || !harnessSessionId) && <fieldset className="mission-tools"><legend>MCP servers · all agent runtimes</legend>{mcpServers.length ? mcpServers.map((server) => <label className="provider-consent" key={server.id}><input type="checkbox" checked={selectedMcpIds.includes(server.id)} onChange={(event) => setSelectedMcpIds((current) => event.target.checked ? [...current, server.id] : current.filter((id) => id !== server.id))} /><span><strong>{server.name}</strong><small>{server.transport} · {server.tools.length} discovered tools · Core artifact capture</small></span></label>) : <p>No enabled MCP profiles. Add one in Settings if this mission needs external tools.</p>}</fieldset>}
            <div className="resource-form-grid">
              <label>Duration (minutes)<input type="number" min={1} max={60} value={durationMinutes} onChange={(event) => setDurationMinutes(Number(event.target.value))} /></label>
              <label>Token limit<input type="number" min={1} max={200000} value={maxTokens} onChange={(event) => setMaxTokens(Number(event.target.value))} /></label>
              <label>Cost limit (USD)<input type="number" min={0} max={100} step="0.01" value={maxCost} onChange={(event) => setMaxCost(Number(event.target.value))} /></label>
              <label>Retries<input type="number" min={0} max={2} value={maxRetries} onChange={(event) => setMaxRetries(Number(event.target.value))} /></label>
            </div>
            <section className="mission-tool-selection">
              <header><div><Wrench size={15} /><span><strong>Command runtime</strong><small>Bash and process I/O are fixed capabilities in every prepared agent session.</small></span></div><span>{automaticTools.length ? "Ready" : "Analysis only"}</span></header>
              {runtimeReady && providerSupportsTools && automaticTools.length
                ? <fieldset className="resource-checklist automatic-tool-list"><legend>Automatically enabled capabilities</legend>{automaticTools.map((name) => <div key={name}><ShieldCheck size={15} /><span><strong>{name}</strong><small>{name === "run_command" ? "session-scoped Bash · project networking optional" : "poll, stdin, and termination"}</small></span></div>)}</fieldset>
                : <div className="mission-tool-empty" role="status"><ShieldCheck size={17} /><p>{toolPreparation === "unavailable" ? toolPreparationDetail : toolSelectionMessage}</p></div>}
              {(automaticTools.length > 0 || selectedMcpIds.length > 0 || runtimeKind === "harness") && <div className="resource-form-grid"><label>Maximum execution calls<input type="number" min={1} max={100} value={maxToolCalls} onChange={(event) => setMaxToolCalls(Number(event.target.value))} /></label><label>Maximum concurrency<input type="number" min={1} max={2} value={maxConcurrency} onChange={(event) => setMaxConcurrency(Number(event.target.value))} /></label></div>}
            </section>
            <p className="provider-dialog-note">{automaticTools.length || selectedMcpIds.length ? "Core applies scope, budgets, capture, and approvals." : "Analysis only · no execution tools"}</p>
          </details>
          {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
          <footer><button className="button secondary" type="button" onClick={() => setOpen(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving || toolPreparation === "preparing" || toolVerificationBusy}>{toolPreparation === "preparing" ? "Checking runtime…" : toolVerificationBusy ? "Checking model…" : saving ? "Starting…" : "Automate task"}</button></footer>
        </form>
      </div>,
      document.body,
    )}
  </>;
}

const terminalStatuses = new Set(["failed", "complete", "cancelled", "interrupted"]);

export function StopMissionButton({ className = "button secondary" }: { className?: string }) {
  const confirm = useConfirmation();
  const { previewMode, run, stopMission } = useWorkspace();
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string>();
  const disabled = previewMode || !run || terminalStatuses.has(run.status) || run.status === "cancelling";
  const stop = async () => {
    if (!run || !await confirm({
      title: "Stop this mission?",
      message: `“${run.title}” will be cancelled after the current safe boundary. Persisted events and evidence will be retained.`,
      confirmLabel: "Stop mission",
      tone: "danger",
    })) return;
    setStopping(true);
    setError(undefined);
    try {
      await stopMission(run.id, { reason: "Stopped by the operator from the workspace" });
    } catch (stopError) {
      void logCaughtDiagnostic("interface.mission_controls.caught_failure_06", "A handled interface operation failed.", stopError, "mission_controls");
      setError(stopError instanceof Error ? stopError.message : "Could not stop the mission.");
    } finally {
      setStopping(false);
    }
  };
  return <span className="mission-stop-control"><button className={className} type="button" disabled={disabled || stopping} onClick={() => void stop()}><Square size={14} /> {stopping ? "Stopping…" : run?.status === "cancelling" ? "Cancelling…" : "Stop mission"}</button>{error && <DiagnosticErrorNotice error={error} fallback="The mission could not be stopped." compact />}</span>;
}

export function DeleteMissionButton({ className = "button quiet danger" }: { className?: string }) {
  const confirm = useConfirmation();
  const { deleteMission, previewMode, run } = useWorkspace();
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string>();
  const disabled = previewMode || !run || !terminalStatuses.has(run.status);
  const remove = async () => {
    if (!run || !await confirm({
      title: "Delete this mission?",
      message: `“${run.title}” and its execution records will be removed from the workspace. Immutable audit events, evidence, and artifacts are retained.`,
      confirmLabel: "Delete mission",
      tone: "danger",
    })) return;
    setDeleting(true);
    setError(undefined);
    try {
      await deleteMission(run.id);
    } catch (deleteError) {
      void logCaughtDiagnostic("interface.mission_controls.caught_failure_07", "A handled interface operation failed.", deleteError, "mission_controls");
      setError(deleteError instanceof Error ? deleteError.message : "Could not delete the mission.");
    } finally {
      setDeleting(false);
    }
  };
  return <span className="mission-stop-control"><button className={className} type="button" disabled={disabled || deleting} title={!run ? "No mission selected" : !terminalStatuses.has(run.status) ? "Stop the mission before deleting it" : undefined} onClick={() => void remove()}><Trash2 size={14} /> {deleting ? "Deleting…" : "Delete mission"}</button>{error && <DiagnosticErrorNotice error={error} fallback="The mission could not be deleted." compact />}</span>;
}
