import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ListTodo, Play, Plus, RotateCcw, ShieldCheck, Square, Trash2, Wrench, X } from "lucide-react";
import { providerModelVerification } from "../api/providerCapabilities";
import { defaultModelRuntime } from "../api/runtimeDefaults";
import type { HarnessProfile, HarnessSessionSummary, McpServerProfile } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { ModalSurface, useConfirmation } from "./DialogSystem";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { InlineValidationNotice } from "./InlineValidationNotice";

interface NewMissionButtonProps {
  className?: string;
  children?: ReactNode;
  showSetupGuidance?: boolean;
}

export function NewMissionButton({ className = "button primary", children, showSetupGuidance = true }: NewMissionButtonProps) {
  const confirm = useConfirmation();
  const { api, coreState, engagement, previewMode, providers, reverifyProvider, startMission } = useWorkspace();
  const availableProviders = useMemo(() => providers.filter((provider) => provider.enabled), [providers]);
  const [runtimeKind, setRuntimeKind] = useState<"native" | "harness">("native");
  const [harnesses, setHarnesses] = useState<HarnessProfile[]>([]);
  const [harnessesLoaded, setHarnessesLoaded] = useState(false);
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
  const [harnessReasoningEffort, setHarnessReasoningEffort] = useState("");
  const [harnessServiceTier, setHarnessServiceTier] = useState("");
  const [stages, setStages] = useState<Array<{ title: string; objective: string }>>([]);
  const [scheduledFor, setScheduledFor] = useState("");
  const [repeatIntervalSeconds, setRepeatIntervalSeconds] = useState(0);
  const [durationMinutes, setDurationMinutes] = useState<number | null>(null);
  const [maxTokens, setMaxTokens] = useState<number | null>(null);
  const [maxCost, setMaxCost] = useState<number | null>(null);
  const [maxRetries, setMaxRetries] = useState(1);
  const [runtimeReady, setRuntimeReady] = useState(false);
  const [runtimeConfigured, setRuntimeConfigured] = useState(false);
  const [maxToolCalls, setMaxToolCalls] = useState<number | null>(null);
  const [maxConcurrency, setMaxConcurrency] = useState(1);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [validationError, setValidationError] = useState<string>();
  const [toolPreparation, setToolPreparation] = useState<"idle" | "preparing" | "ready" | "unavailable">("idle");
  const [toolPreparationDetail, setToolPreparationDetail] = useState<string>();
  const [toolVerificationBusy, setToolVerificationBusy] = useState(false);
  const attemptedToolVerificationRef = useRef(new Set<string>());
  const runtimeDefaultAppliedRef = useRef(false);
  const selectedHarness = harnesses.find((item) => item.id === harnessId);
  const attachedHarnessSession = harnessSessions.find((item) => item.id === harnessSessionId);
  const selectedModelOptions = selectedHarness?.modelOptions?.find((item) => item.model === model);
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
    const next = availableProviders.find(
      (item) => item.state === "healthy" || item.state === "unchecked",
    );
    setProviderId(next?.id ?? "");
    if (runtimeKind === "native") setModel(next?.models[0] ?? "");
  }, [availableProviders, providerId, runtimeKind]);

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
        setHarnessesLoaded(true);
      })
      .catch((caughtError) => {
        void logCaughtDiagnostic("interface.mission_controls.caught_failure_01", "A handled interface operation failed.", caughtError, "mission_controls"); if (active) { setHarnesses([]); setMcpServers([]); setHarnessSessions([]); setHarnessesLoaded(true); } });
    return () => { active = false; };
  }, [api, coreState, engagement?.id]);

  useEffect(() => {
    if (!harnessesLoaded || runtimeDefaultAppliedRef.current) return;
    const selection = defaultModelRuntime(availableProviders, harnesses);
    if (!selection) return;
    runtimeDefaultAppliedRef.current = true;
    setRuntimeKind(selection.kind === "harness" ? "harness" : "native");
    setModel(selection.model);
    if (selection.kind === "harness") setHarnessId(selection.id);
    else setProviderId(selection.id);
  }, [availableProviders, harnesses, harnessesLoaded]);

  useEffect(() => {
    if (runtimeKind !== "harness") return;
    const attached = harnessSessions.find((item) => item.id === harnessSessionId);
    const harness = harnesses.find((item) => item.id === (attached?.harnessProfileId ?? harnessId));
    if (attached) setHarnessId(attached.harnessProfileId);
    setModel(attached?.model ?? harness?.models[0] ?? "");
    const options = harness?.modelOptions?.find((item) => item.model === (attached?.model ?? harness?.models[0] ?? ""));
    setHarnessReasoningEffort(attached?.reasoningEffort ?? options?.defaultReasoningEffort ?? "");
    setHarnessServiceTier(attached?.serviceTier ?? options?.defaultServiceTier ?? "");
  }, [harnessId, harnessSessionId, harnessSessions, harnesses, runtimeKind]);

  useEffect(() => {
    if (runtimeKind !== "harness" || harnessSessionId) return;
    setHarnessReasoningEffort(selectedModelOptions?.defaultReasoningEffort ?? "");
    setHarnessServiceTier(selectedModelOptions?.defaultServiceTier ?? "");
  }, [harnessSessionId, runtimeKind, selectedModelOptions]);

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
  const automaticTools = useMemo(() => runtimeReady && (runtimeKind === "harness" || providerSupportsTools)
    ? ["run_command", "process_io"]
    : [], [providerSupportsTools, runtimeKind, runtimeReady]);
  const runtimeCanExecute = automaticTools.length > 0 || selectedMcpIds.length > 0;
  const toolSelectionMessage = toolVerificationBusy
    ? `Checking tool support for ${model.trim()}…`
    : toolPreparation === "preparing"
    ? toolPreparationDetail ?? "Checking the command runtime…"
    : !runtimeConfigured
    ? coreState !== "online"
      ? "Nebula Core is offline; reconnect Core before using command execution."
      : "The pinned automation runtime is not configured."
    : runtimeKind !== "harness" && !providerSupportsTools
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
    setModel(next?.models[0] ?? "");
  };

  const openMission = () => {
    setError(undefined);
    setValidationError(undefined);
    setMaxToolCalls(null);
    setMaxConcurrency(automaticTools.length ? 2 : 1);
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    setMaxToolCalls(null);
    setMaxConcurrency(runtimeKind === "native" && (automaticTools.length || selectedMcpIds.length) ? 2 : 1);
  }, [automaticTools, open, runtimeKind, selectedMcpIds.length]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const cleanName = name.trim();
    const cleanObjective = objective.trim();
    const cleanModel = model.trim();
    if (!engagement) {
      setValidationError("Select a project before starting a mission.");
      return;
    }
    if (runtimeKind === "native" && !provider) {
      setValidationError("Select an enabled provider before starting a mission.");
      return;
    }
    if (runtimeKind === "harness" && !selectedHarness) {
      setValidationError("Select an enabled agent harness before starting a mission.");
      return;
    }
    if (!cleanName) {
      setValidationError("Enter a mission name so you can identify it later.");
      return;
    }
    if (!cleanObjective) {
      setValidationError("Enter a mission objective.");
      return;
    }
    if (!cleanModel) {
      setValidationError("Select a model for this mission.");
      return;
    }
    const cleanStages = stages.map((stage) => ({ title: stage.title.trim(), objective: stage.objective.trim() }));
    if (cleanStages.some((stage) => !stage.title || !stage.objective)) {
      setValidationError("Every mission stage needs both a name and an objective.");
      return;
    }
    const scheduledDate = scheduledFor ? new Date(scheduledFor) : undefined;
    if (scheduledDate && (Number.isNaN(scheduledDate.getTime()) || scheduledDate.getTime() <= Date.now())) {
      setValidationError("Choose a future start time for scheduled work.");
      return;
    }
    if (repeatIntervalSeconds && !scheduledDate) {
      setValidationError("Choose a start time before enabling a repeating schedule.");
      return;
    }
    if (durationMinutes !== null && (!Number.isInteger(durationMinutes) || durationMinutes < 1)) {
      setValidationError("Duration must be a positive whole number of minutes.");
      return;
    }
    if (maxTokens !== null && (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 200_000)) {
      setValidationError("Token limit must be a whole number from 1 to 200,000.");
      return;
    }
    if (maxCost !== null && (!Number.isFinite(maxCost) || maxCost < 0 || maxCost > 100)) {
      setValidationError("Cost limit must be from $0 to $100.");
      return;
    }
    if (!Number.isInteger(maxRetries) || maxRetries < 0 || maxRetries > 2) {
      setValidationError("Retries must be a whole number from 0 to 2.");
      return;
    }
    if ((runtimeKind === "harness" || automaticTools.length || selectedMcpIds.length) && maxToolCalls !== null && (!Number.isInteger(maxToolCalls) || maxToolCalls < 1 || maxToolCalls > 100)) {
      setValidationError("Maximum tool calls must be a whole number from 1 to 100.");
      return;
    }
    if ((automaticTools.length || selectedMcpIds.length) && (!Number.isInteger(maxConcurrency) || maxConcurrency < 1 || maxConcurrency > 2)) {
      setValidationError("Maximum concurrency must be 1 or 2.");
      return;
    }
    const selectedHarnessSession = harnessSessions.find((item) => item.id === harnessSessionId);
    const runtimeUsesMcp = runtimeKind === "harness"
      ? Boolean(harnessSessionId ? selectedHarnessSession?.mcpServerIds.length : selectedMcpIds.length)
      : selectedMcpIds.length > 0;
    if (runtimeKind === "native" && runtimeUsesMcp && !providerSupportsTools) {
      setValidationError(`Tool calling must be verified for ${cleanModel} before selecting MCP tools.`);
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
        setValidationError("This runtime profile is text-only. Permit project/document data in Settings or remove MCP servers.");
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
    setValidationError(undefined);
    const optionalBudget = {
      ...(durationMinutes === null ? {} : { maxDurationSeconds: durationMinutes * 60 }),
      ...(maxTokens === null ? {} : { maxTokens }),
      ...(maxCost === null ? {} : { maxCostUsd: maxCost }),
      ...(
        maxToolCalls !== null
        && (runtimeKind === "harness" || automaticTools.length || selectedMcpIds.length)
          ? { maxToolCalls }
          : {}
      ),
    };
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
        harnessReasoningEffort: harnessReasoningEffort || undefined,
        harnessServiceTier: harnessServiceTier || undefined,
        stages: cleanStages,
        scheduledFor: scheduledDate?.toISOString(),
        repeatIntervalSeconds: repeatIntervalSeconds || undefined,
        ...optionalBudget,
        maxRetries: 0,
        maxConcurrency: 1,
        allowCloudToolResults,
      } : { engagementId: engagement.id, name: cleanName, objective: cleanObjective, backend: "native", providerId: provider?.id, mcpServerIds: selectedMcpIds, model: cleanModel, stages: cleanStages, scheduledFor: scheduledDate?.toISOString(), repeatIntervalSeconds: repeatIntervalSeconds || undefined, ...optionalBudget, maxRetries, maxConcurrency: automaticTools.length || selectedMcpIds.length ? maxConcurrency : 1, allowCloudToolResults });
      setOpen(false);
      setName("");
      setObjective("");
      setStages([]);
      setScheduledFor("");
      setRepeatIntervalSeconds(0);
      setDurationMinutes(null);
      setMaxTokens(null);
      setMaxCost(null);
      setMaxToolCalls(null);
      setMaxConcurrency(1);
      setMaxRetries(1);
    } catch (startError) {
      void logCaughtDiagnostic("interface.mission_controls.caught_failure_05", "A handled interface operation failed.", startError, "mission_controls");
      setError(startError instanceof Error ? startError.message : "Could not start the mission.");
    } finally {
      setSaving(false);
    }
  };

  return <>
    <button className={className} type="button" disabled={previewMode || !engagement || (availableProviders.length === 0 && harnesses.length === 0)} title={availableProviders.length || harnesses.length ? undefined : "Add an enabled provider or agent harness before automating a task"} onClick={openMission}>{children ?? <><Play size={16} /> Automate task</>}</button>
    {showSetupGuidance && harnessesLoaded && availableProviders.length === 0 && harnesses.length === 0 && <span className="mission-runtime-setup" role="status"><span>Missions need an enabled model provider or agent harness with a verified model.</span><a href="/settings#models-settings">Configure runtime</a></span>}
    {open && createPortal(
        <ModalSurface as="form" noValidate className="provider-dialog resource-dialog mission-dialog" labelledBy="mission-dialog-title" onClose={() => { if (!saving && !toolVerificationBusy && toolPreparation !== "preparing") setOpen(false); }} onSubmit={(event) => void submit(event)}>
          <header>
            <div><small>{runtimeCanExecute ? "Supervised security automation" : "Analysis-only automation"}</small><h2 id="mission-dialog-title">Automate task</h2></div>
            <button className="icon-button subtle" type="button" aria-label="Close automation dialog" onClick={() => setOpen(false)}><X size={17} /></button>
          </header>
          <label>Mission name<input required autoFocus maxLength={300} value={name} placeholder="Quarterly perimeter review" onChange={(event) => { setName(event.target.value); setError(undefined); }} /></label>
          <label>Objective<textarea required rows={5} value={objective} placeholder="Describe the outcome you want Nebula to produce…" onChange={(event) => { setObjective(event.target.value); setError(undefined); }} /></label>
          <details className="provider-advanced mission-advanced">
            <summary>Advanced</summary>
            <label>Runtime<select aria-label="Mission runtime" value={runtimeKind} onChange={(event) => { const next = event.target.value as "native" | "harness"; runtimeDefaultAppliedRef.current = true; setRuntimeKind(next); setHarnessSessionId(""); setSelectedMcpIds([]); if (next === "native") selectProvider(providerId || availableProviders[0]?.id || ""); }}><option value="native" disabled={availableProviders.length === 0}>Native mission{availableProviders.length === 0 ? " · unavailable" : ""}</option><option value="harness" disabled={harnesses.length === 0}>Agent harness{harnesses.length === 0 ? " · unavailable" : ""}</option></select></label>
            {runtimeKind === "native" ? <label>Provider<select value={providerId} onChange={(event) => { selectProvider(event.target.value); setError(undefined); }}>{availableProviders.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label> : <><label>Harness<select aria-label="Mission harness" value={harnessId} disabled={Boolean(harnessSessionId)} onChange={(event) => { setHarnessId(event.target.value); setError(undefined); }}>{harnesses.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>Session<select aria-label="Harness session" value={harnessSessionId} onChange={(event) => setHarnessSessionId(event.target.value)}><option value="">Start a new session</option>{harnessSessions.filter((item) => item.harnessProfileId === harnessId || item.id === harnessSessionId).map((item) => <option value={item.id} key={item.id}>{item.model} · {item.status}</option>)}</select></label></>}
            <label>Model<select required value={model} disabled={Boolean(harnessSessionId) || !modelOptions.length} onChange={(event) => { setModel(event.target.value); setError(undefined); }}><option value="">{modelOptions.length ? "Select model" : runtimeKind === "harness" ? "Run a harness check to discover models" : "Run provider health check to discover models"}</option>{modelOptions.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
            {runtimeKind === "harness" && (selectedModelOptions?.reasoningEfforts.length || harnessReasoningEffort) ? <label>Effort<select aria-label="Mission harness effort" value={harnessReasoningEffort} disabled={Boolean(harnessSessionId)} onChange={(event) => setHarnessReasoningEffort(event.target.value)}><option value="">Harness default</option>{harnessReasoningEffort && !selectedModelOptions?.reasoningEfforts.some((item) => item.id === harnessReasoningEffort) && <option value={harnessReasoningEffort}>{harnessReasoningEffort} · saved</option>}{selectedModelOptions?.reasoningEfforts.map((item) => <option value={item.id} title={item.description || undefined} key={item.id}>{item.label}</option>)}</select></label> : null}
            {runtimeKind === "harness" && (selectedModelOptions?.serviceTiers.length || harnessServiceTier) ? <label>Speed<select aria-label="Mission harness speed" value={harnessServiceTier} disabled={Boolean(harnessSessionId)} onChange={(event) => setHarnessServiceTier(event.target.value)}><option value="">Harness default</option>{harnessServiceTier && !selectedModelOptions?.serviceTiers.some((item) => item.id === harnessServiceTier) && <option value={harnessServiceTier}>{harnessServiceTier} · saved</option>}{selectedModelOptions?.serviceTiers.map((item) => <option value={item.id} title={item.description || undefined} key={item.id}>{item.label}</option>)}</select></label> : null}
            <section className="mission-stage-builder" aria-labelledby="mission-stages-title">
              <header><div><ListTodo size={15} /><span><strong id="mission-stages-title">Stages</strong><small>Optional checkpoints executed in order with a durable result per stage.</small></span></div><button className="button quiet" type="button" disabled={stages.length >= 12} onClick={() => setStages((current) => [...current, { title: `Stage ${current.length + 1}`, objective: "" }])}><Plus size={14} /> Add stage</button></header>
              {stages.map((stage, index) => <fieldset className="mission-stage" key={index}><legend>Stage {index + 1}</legend><label>Name<input value={stage.title} maxLength={300} onChange={(event) => setStages((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, title: event.target.value } : item))} /></label><label>Objective<textarea rows={3} value={stage.objective} maxLength={10_000} onChange={(event) => setStages((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, objective: event.target.value } : item))} /></label><button className="icon-button subtle danger" type="button" aria-label={`Remove stage ${index + 1}`} onClick={() => setStages((current) => current.filter((_, itemIndex) => itemIndex !== index))}><Trash2 size={15} /></button></fieldset>)}
            </section>
            <section className="mission-schedule" aria-labelledby="mission-schedule-title"><header><strong id="mission-schedule-title">Schedule</strong><small>Core owns the start time; scheduled work survives page closure and Core restarts.</small></header><div className="resource-form-grid"><label>Start time<input type="datetime-local" value={scheduledFor} min={new Date(Date.now() + 60_000).toISOString().slice(0, 16)} onChange={(event) => setScheduledFor(event.target.value)} /></label><label>Repeat<select value={repeatIntervalSeconds} disabled={!scheduledFor} onChange={(event) => setRepeatIntervalSeconds(Number(event.target.value))}><option value={0}>Do not repeat</option><option value={86400}>Daily</option><option value={604800}>Weekly</option></select></label></div>{repeatIntervalSeconds > 0 && <small>Each occurrence becomes a new audited Mission. It never reuses an uncertain in-flight run.</small>}</section>
            {(runtimeKind === "native" || !harnessSessionId) && <fieldset className="mission-tools"><legend>MCP servers · all agent runtimes</legend>{mcpServers.length ? mcpServers.map((server) => <label className="provider-consent" key={server.id}><input type="checkbox" checked={selectedMcpIds.includes(server.id)} onChange={(event) => setSelectedMcpIds((current) => event.target.checked ? [...current, server.id] : current.filter((id) => id !== server.id))} /><span><strong>{server.name}</strong><small>{server.transport} · {server.tools.length} discovered tools · Core artifact capture</small></span></label>) : <p>No enabled MCP profiles. Add one in Settings if this mission needs external tools.</p>}</fieldset>}
            <div className="resource-form-grid">
              <label>Duration (minutes)<small id="mission-duration-unlimited-help">Leave blank for unlimited (default)</small><input aria-label="Duration (minutes)" aria-describedby="mission-duration-unlimited-help" type="number" min={1} placeholder="Unlimited" value={durationMinutes ?? ""} onChange={(event) => setDurationMinutes(event.target.value === "" ? null : Number(event.target.value))} /></label>
              <label>Token limit<small id="mission-token-unlimited-help">Leave blank for unlimited (default)</small><input aria-label="Token limit" aria-describedby="mission-token-unlimited-help" type="number" min={1} max={200000} placeholder="Unlimited" value={maxTokens ?? ""} onChange={(event) => setMaxTokens(event.target.value === "" ? null : Number(event.target.value))} /></label>
              <label>Cost limit (USD)<small id="mission-cost-unlimited-help">Leave blank for unlimited (default)</small><input aria-label="Cost limit (USD)" aria-describedby="mission-cost-unlimited-help" type="number" min={0} max={100} step="0.01" placeholder="Unlimited" value={maxCost ?? ""} onChange={(event) => setMaxCost(event.target.value === "" ? null : Number(event.target.value))} /></label>
              <label>Retries<input type="number" min={0} max={2} value={maxRetries} onChange={(event) => setMaxRetries(Number(event.target.value))} /></label>
            </div>
            <section className="mission-tool-selection">
              <header><div><Wrench size={15} /><span><strong>Command runtime</strong><small>Bash and process I/O use Nebula's pinned automation runtime.</small></span></div><span>{automaticTools.length ? "Ready" : "Analysis only"}</span></header>
              {runtimeReady && automaticTools.length
                ? <fieldset className="resource-checklist automatic-tool-list"><legend>Automatically enabled capabilities</legend>{automaticTools.map((name) => <div key={name}><ShieldCheck size={15} /><span><strong>{name}</strong><small>{name === "run_command" ? "session-scoped Bash · project networking optional" : "poll, stdin, and termination"}</small></span></div>)}</fieldset>
                : <div className="mission-tool-empty" role="status"><ShieldCheck size={17} /><p>{toolPreparation === "unavailable" ? toolPreparationDetail : toolSelectionMessage}</p></div>}
              {(automaticTools.length > 0 || selectedMcpIds.length > 0 || runtimeKind === "harness") && <div className="resource-form-grid"><label>Maximum execution calls<small id="mission-tool-unlimited-help">Leave blank for unlimited (default)</small><input aria-label="Maximum execution calls" aria-describedby="mission-tool-unlimited-help" type="number" min={1} max={100} placeholder="Unlimited" value={maxToolCalls ?? ""} onChange={(event) => setMaxToolCalls(event.target.value === "" ? null : Number(event.target.value))} /></label><label>Maximum concurrency<input type="number" min={1} max={2} value={maxConcurrency} onChange={(event) => setMaxConcurrency(Number(event.target.value))} /></label></div>}
            </section>
            <p className="provider-dialog-note">{runtimeCanExecute ? "Core applies scope, budgets, capture, and approvals." : "Analysis only · no execution tools"}</p>
          </details>
          {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
          {validationError && <InlineValidationNotice message={validationError} />}
          <footer><button className="button secondary" type="button" onClick={() => setOpen(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving || toolPreparation === "preparing" || toolVerificationBusy}>{toolPreparation === "preparing" ? "Checking runtime…" : toolVerificationBusy ? "Checking model…" : saving ? "Starting…" : "Automate task"}</button></footer>
        </ModalSurface>,
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

export function RetryMissionButton({ className = "button secondary" }: { className?: string }) {
  const confirm = useConfirmation();
  const { previewMode, retryMission, run } = useWorkspace();
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState<string>();
  const eligible = Boolean(run && terminalStatuses.has(run.status));
  const retry = async () => {
    if (!run) return;
    const accepted = await confirm({
      title: "Start a new Mission from this one?",
      message: run.remoteMcpConfirmed
        ? "Nebula will create a new audited run with the same frozen runtime, stages, and MCP servers. Remote MCP result transfer must be authorized again. The interrupted run remains unchanged."
        : "Nebula will create a new audited run with the same frozen runtime, stages, and limits. The original run remains unchanged.",
      confirmLabel: "Start retry",
    });
    if (!accepted) return;
    setRetrying(true);
    setError(undefined);
    try {
      await retryMission(run.id, run.remoteMcpConfirmed === true);
    } catch (retryError) {
      void logCaughtDiagnostic("interface.mission_controls.retry_failed", "A Mission retry failed.", retryError, "mission_controls");
      setError(retryError instanceof Error ? retryError.message : "Could not retry the Mission.");
    } finally {
      setRetrying(false);
    }
  };
  return <span className="mission-stop-control"><button className={className} type="button" disabled={previewMode || !eligible || retrying} onClick={() => void retry()}><RotateCcw size={14} /> {retrying ? "Starting…" : "Retry mission"}</button>{error && <DiagnosticErrorNotice error={error} fallback="The Mission could not be retried." compact />}</span>;
}
