import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Play, ShieldCheck, Square, Wrench, X } from "lucide-react";
import { providerModelVerification } from "../api/providerCapabilities";
import type { HarnessProfile, HarnessSessionSummary, McpServerProfile, ToolSummary } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { notifyToolPacksChanged, useToolPackRevision } from "../state/toolPackChanges";
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
  const [objective, setObjective] = useState("");
  const [providerId, setProviderId] = useState("");
  const provider = availableProviders.find((item) => item.id === providerId);
  const [model, setModel] = useState("");
  const [durationMinutes, setDurationMinutes] = useState(60);
  const [maxTokens, setMaxTokens] = useState(20_000);
  const [maxCost, setMaxCost] = useState(10);
  const [maxRetries, setMaxRetries] = useState(1);
  const [assignedTools, setAssignedTools] = useState<ToolSummary[]>([]);
  const [toolConfigurationAvailable, setToolConfigurationAvailable] = useState(false);
  const [maxToolCalls, setMaxToolCalls] = useState(0);
  const [maxConcurrency, setMaxConcurrency] = useState(1);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [toolPreparation, setToolPreparation] = useState<"idle" | "preparing" | "ready" | "unavailable">("idle");
  const [toolPreparationDetail, setToolPreparationDetail] = useState<string>();
  const [toolVerificationBusy, setToolVerificationBusy] = useState(false);
  const toolPreparationRef = useRef<Promise<void> | undefined>(undefined);
  const attemptedToolVerificationRef = useRef(new Set<string>());
  const toolPackRevision = useToolPackRevision();

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
    setModel(attached?.model ?? harness?.defaultModel ?? "");
  }, [harnessId, harnessSessionId, harnessSessions, harnesses, runtimeKind]);

  useEffect(() => {
    let active = true;
    setMaxToolCalls(0);
    setMaxConcurrency(1);
    if (!api || coreState !== "online" || !engagement) {
      setAssignedTools([]);
      setToolConfigurationAvailable(false);
      return () => { active = false; };
    }
    void Promise.all([api.listEngagementToolAssignments(engagement.id), api.listTools(), api.listToolPacks()])
      .then(([assignments, tools, packs]) => {
        if (!active) return;
        const readyDigests = new Set(packs
          .filter((pack) => pack.status === "ready")
          .map((pack) => pack.manifestDigest));
        setToolConfigurationAvailable(true);
        setAssignedTools(tools.filter((tool) => assignments.some((assignment) => assignment.enabled
          && assignment.manifestDigest !== undefined
          && readyDigests.has(assignment.manifestDigest)
          && assignment.manifestDigest === tool.packManifestDigest
          && assignment.toolNames.includes(tool.name))));
      })
      .catch((caughtError) => {
        void logCaughtDiagnostic("interface.mission_controls.caught_failure_02", "A handled interface operation failed.", caughtError, "mission_controls");
        if (!active) return;
        setAssignedTools([]);
        setToolConfigurationAvailable(false);
      });
    return () => { active = false; };
  }, [api, coreState, engagement?.id, toolPackRevision]);

  const verification = providerModelVerification(provider, model);
  const providerSupportsTools = verification?.status === "verified";
  const automaticTools = useMemo(() => providerSupportsTools
    ? assignedTools.filter((tool) => tool.available).map((tool) => tool.name)
    : [], [assignedTools, providerSupportsTools]);
  const toolSelectionMessage = toolVerificationBusy
    ? `Checking tool support for ${model.trim()}…`
    : toolPreparation === "preparing"
    ? toolPreparationDetail ?? "Preparing the official signed Toolbox for this project…"
    : !toolConfigurationAvailable
    ? coreState !== "online"
      ? "Nebula Core is offline; reconnect Core before configuring Toolbox capabilities."
      : "Toolbox configuration APIs are unavailable in this Core."
    : !providerSupportsTools
      ? verification?.status === "failed"
        ? `Tool verification failed for ${model}: ${verification.failureDetail ?? "the provider did not return a valid structured call"}. Reverify it in Settings.`
        : model
          ? `Tool calling has not been verified for ${model}. Verify it in Settings.`
          : "Select an exact model and verify tool calling in Settings."
      : assignedTools.length === 0
        ? "No ready Toolbox capabilities are assigned to this engagement."
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

  const ensureOfficialToolbox = () => {
    if (!api || coreState !== "online" || !engagement || toolPreparationRef.current) return;
    const operation = (async () => {
      setToolPreparation("preparing");
      setToolPreparationDetail("Checking the official Toolbox…");
      try {
        const [assignments, installedTools, installedPacks] = await Promise.all([
          api.listEngagementToolAssignments(engagement.id),
          api.listTools(),
          api.listToolPacks(),
        ]);
        const readyDigests = new Set(installedPacks
          .filter((pack) => pack.status === "ready"
            && pack.trustState === "trusted"
            && pack.publisher === "berylliumsec"
            && pack.name === "nebula-toolbox")
          .map((pack) => pack.manifestDigest));
        const currentTools = installedTools.filter((tool) => assignments.some((assignment) => assignment.enabled
          && assignment.manifestDigest !== undefined
          && readyDigests.has(assignment.manifestDigest)
          && assignment.manifestDigest === tool.packManifestDigest
          && assignment.toolNames.includes(tool.name)));
        if (currentTools.length) {
          setAssignedTools(currentTools);
          setToolConfigurationAvailable(true);
          setToolPreparation("ready");
          setToolPreparationDetail(undefined);
          return;
        }

        const [catalog, runners] = await Promise.all([
          api.listToolCatalog(),
          api.listRunnerProfiles(),
        ]);
        const officialEntries = catalog.filter((entry) => entry.signed
          && entry.publisher === "berylliumsec"
          && (entry.collectionId === "nebula-toolbox" || entry.name === "nebula-toolbox"));
        const runner = runners.find((candidate) => candidate.state === "ready");
        if (!officialEntries.length || !runner) {
          throw new Error(!runner
            ? "A verified local runtime is required before Toolbox can be prepared."
            : "The signed Nebula Toolbox is not published in the configured catalog yet.");
        }

        setToolPreparationDetail("Downloading and verifying the official Toolbox…");
        const officialDigests = new Set(officialEntries.map((entry) => entry.manifestDigest));
        let readyOfficialPacks = installedPacks.filter((pack) => pack.status === "ready"
          && pack.trustState === "trusted"
          && officialDigests.has(pack.manifestDigest));
        if (readyOfficialPacks.length < officialEntries.length) {
          const collectionId = officialEntries.find((entry) => entry.collectionId)?.collectionId;
          const installed = collectionId
            ? await api.installToolCollection(collectionId, runner.id)
            : [await api.installToolPack(officialEntries[0].id, runner.id, officialEntries[0].version)];
          readyOfficialPacks = installed.filter((pack) => pack.status === "ready"
            && pack.trustState === "trusted"
            && officialDigests.has(pack.manifestDigest));
        }
        if (!readyOfficialPacks.length) {
          throw new Error("The official Toolbox did not reach a verified ready state.");
        }

        setToolPreparationDetail("Assigning verified capabilities to this project…");
        const latestTools = await api.listTools();
        const savedAssignments = await Promise.all(readyOfficialPacks.map((pack) => api.updateEngagementToolAssignment(
          engagement.id,
          {
            manifestDigest: pack.manifestDigest,
            toolNames: latestTools
              .filter((tool) => tool.available && tool.packManifestDigest === pack.manifestDigest)
              .map((tool) => tool.name),
            enabled: true,
          },
        )));
        const preparedTools = latestTools.filter((tool) => savedAssignments.some((assignment) => assignment.enabled
          && assignment.manifestDigest === tool.packManifestDigest
          && assignment.toolNames.includes(tool.name)));
        setAssignedTools(preparedTools);
        setToolConfigurationAvailable(true);
        setToolPreparation("ready");
        setToolPreparationDetail(undefined);
        notifyToolPacksChanged();
      } catch (preparationError) {
        void logCaughtDiagnostic("interface.mission_controls.caught_failure_04", "A handled interface operation failed.", preparationError, "mission_controls");
        setToolPreparation("unavailable");
        setToolPreparationDetail(preparationError instanceof Error ? preparationError.message : "Could not prepare the official Toolbox.");
      }
    })();
    toolPreparationRef.current = operation;
    void operation.finally(() => {
      toolPreparationRef.current = undefined;
    });
  };

  const selectProvider = (id: string) => {
    const next = availableProviders.find((item) => item.id === id);
    setProviderId(id);
    setModel(next?.defaultModel ?? next?.models[0] ?? "");
  };

  const openMission = () => {
    setError(undefined);
    setMaxToolCalls(runtimeKind === "harness" ? 50 : automaticTools.length ? 50 : 0);
    setMaxConcurrency(automaticTools.length ? 2 : 1);
    setOpen(true);
    ensureOfficialToolbox();
  };

  useEffect(() => {
    if (!open) return;
    setMaxToolCalls(runtimeKind === "harness" ? 50 : automaticTools.length ? 50 : 0);
    setMaxConcurrency(runtimeKind === "native" && automaticTools.length ? 2 : 1);
  }, [automaticTools, open, runtimeKind]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const cleanObjective = objective.trim();
    const cleanModel = model.trim();
    if (!engagement) {
      setError("Select an engagement before starting a mission.");
      return;
    }
    const selectedHarness = harnesses.find((item) => item.id === harnessId);
    if (runtimeKind === "native" && !provider) {
      setError("Select an enabled provider before starting a mission.");
      return;
    }
    if (runtimeKind === "harness" && !selectedHarness) {
      setError("Select an enabled agent harness before starting a mission.");
      return;
    }
    if (!cleanObjective) {
      setError("Enter a mission objective.");
      return;
    }
    if (!cleanModel) {
      setError("Enter the exact model ID for this mission.");
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
    if ((runtimeKind === "harness" || automaticTools.length) && (!Number.isInteger(maxToolCalls) || maxToolCalls < 1 || maxToolCalls > 100)) {
      setError("Maximum tool calls must be a whole number from 1 to 100.");
      return;
    }
    if (automaticTools.length && (!Number.isInteger(maxConcurrency) || maxConcurrency < 1 || maxConcurrency > 2)) {
      setError("Maximum concurrency must be 1 or 2.");
      return;
    }
    const selectedHarnessSession = harnessSessions.find((item) => item.id === harnessSessionId);
    const harnessUsesMcp = runtimeKind === "harness" && Boolean(
      harnessSessionId ? selectedHarnessSession?.mcpServerIds.length : selectedMcpIds.length,
    );
    let allowCloudToolResults = false;
    if (harnessUsesMcp && selectedHarness && !selectedHarness.localOnly) {
      if (!selectedHarness.permitsSensitiveData) {
        setError("This harness profile is text-only. Permit project/document data in Settings or remove MCP servers.");
        return;
      }
      allowCloudToolResults = await confirm({
        title: "Allow MCP results in this mission?",
        message: `Allow bounded MCP tool inputs and results to reach ${selectedHarness.name} for this mission? Every risky call still follows its exact approval policy.`,
        confirmLabel: "Allow this mission",
      });
      if (!allowCloudToolResults) return;
    }
    setSaving(true);
    setError(undefined);
    try {
      await startMission(runtimeKind === "harness" ? {
        engagementId: engagement.id,
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
      } : { engagementId: engagement.id, objective: cleanObjective, backend: "native", providerId: provider?.id, model: cleanModel, maxDurationSeconds: durationMinutes * 60, maxTokens, maxCostUsd: maxCost, maxRetries, toolNames: automaticTools, maxToolCalls: automaticTools.length ? maxToolCalls : 0, maxConcurrency: automaticTools.length ? maxConcurrency : 1 });
      setOpen(false);
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
            <div><small>{automaticTools.length ? "Supervised automation" : "Analysis-only automation"}</small><h2 id="mission-dialog-title">Automate task</h2></div>
            <button className="icon-button subtle" type="button" aria-label="Close automation dialog" onClick={() => setOpen(false)}><X size={17} /></button>
          </header>
          <label>Objective<textarea required autoFocus rows={5} value={objective} placeholder="Describe the outcome you want Nebula to produce…" onChange={(event) => { setObjective(event.target.value); setError(undefined); }} /></label>
          <details className="provider-advanced mission-advanced">
            <summary>Advanced</summary>
            <label>Runtime<select aria-label="Mission runtime" value={runtimeKind} onChange={(event) => { const next = event.target.value as "native" | "harness"; setRuntimeKind(next); setHarnessSessionId(""); setSelectedMcpIds([]); if (next === "native") selectProvider(providerId || availableProviders[0]?.id || ""); }}><option value="native">Native mission</option><option value="harness">Agent harness</option></select></label>
            {runtimeKind === "native" ? <label>Provider<select value={providerId} onChange={(event) => { selectProvider(event.target.value); setError(undefined); }}>{availableProviders.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label> : <><label>Harness<select aria-label="Mission harness" value={harnessId} disabled={Boolean(harnessSessionId)} onChange={(event) => { setHarnessId(event.target.value); setError(undefined); }}>{harnesses.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>Session<select aria-label="Harness session" value={harnessSessionId} onChange={(event) => setHarnessSessionId(event.target.value)}><option value="">Start a new session</option>{harnessSessions.filter((item) => item.harnessProfileId === harnessId || item.id === harnessSessionId).map((item) => <option value={item.id} key={item.id}>{item.model} · {item.status}</option>)}</select></label></>}
            <label>Model<input required value={model} list="mission-models" disabled={Boolean(harnessSessionId)} placeholder="Exact model ID" onChange={(event) => { setModel(event.target.value); setError(undefined); }} /><datalist id="mission-models">{runtimeKind === "native" && provider?.models.map((item) => <option value={item} key={item} />)}</datalist></label>
            {runtimeKind === "harness" && !harnessSessionId && <fieldset className="mission-tools"><legend>MCP servers</legend>{mcpServers.length ? mcpServers.map((server) => <label className="provider-consent" key={server.id}><input type="checkbox" checked={selectedMcpIds.includes(server.id)} onChange={(event) => setSelectedMcpIds((current) => event.target.checked ? [...current, server.id] : current.filter((id) => id !== server.id))} /><span><strong>{server.name}</strong><small>{server.transport} · {server.tools.length} discovered tools</small></span></label>) : <p>No enabled MCP profiles. Add one in Settings if this mission needs external tools.</p>}</fieldset>}
            <div className="resource-form-grid">
              <label>Duration (minutes)<input type="number" min={1} max={60} value={durationMinutes} onChange={(event) => setDurationMinutes(Number(event.target.value))} /></label>
              <label>Token limit<input type="number" min={1} max={200000} value={maxTokens} onChange={(event) => setMaxTokens(Number(event.target.value))} /></label>
              <label>Cost limit (USD)<input type="number" min={0} max={100} step="0.01" value={maxCost} onChange={(event) => setMaxCost(Number(event.target.value))} /></label>
              <label>Retries<input type="number" min={0} max={2} value={maxRetries} onChange={(event) => setMaxRetries(Number(event.target.value))} /></label>
            </div>
            <section className="mission-tool-selection">
              <header><div><Wrench size={15} /><span><strong>Toolbox automatic</strong><small>Verified assigned capabilities are enabled automatically.</small></span></div><span>{automaticTools.length ? `${automaticTools.length} enabled` : "Analysis only"}</span></header>
              {toolConfigurationAvailable && providerSupportsTools && automaticTools.length
                ? <fieldset className="resource-checklist automatic-tool-list"><legend>Automatically enabled capabilities</legend>{assignedTools.filter((tool) => tool.available).map((tool) => <div key={tool.name}><ShieldCheck size={15} /><span><strong>{tool.name}</strong><small>{tool.riskClass.replaceAll("_", " ")}{tool.requiresApproval ? " · approval required" : ""}</small></span></div>)}</fieldset>
                : <div className="mission-tool-empty" role="status"><ShieldCheck size={17} /><p>{toolPreparation === "unavailable" ? toolPreparationDetail : toolSelectionMessage}</p></div>}
              {automaticTools.length > 0 && <div className="resource-form-grid"><label>Maximum tool calls<input type="number" min={1} max={100} value={maxToolCalls} onChange={(event) => setMaxToolCalls(Number(event.target.value))} /></label><label>Maximum concurrency<input type="number" min={1} max={2} value={maxConcurrency} onChange={(event) => setMaxConcurrency(Number(event.target.value))} /></label></div>}
            </section>
            <p className="provider-dialog-note">{automaticTools.length ? "All available assigned capabilities are enabled automatically. Core enforces project scope, container isolation, budgets, evidence capture, and high-risk approvals." : "This task is analysis-only and receives no execution capabilities."}</p>
          </details>
          {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
          <footer><button className="button secondary" type="button" onClick={() => setOpen(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving || toolPreparation === "preparing" || toolVerificationBusy}>{toolPreparation === "preparing" ? "Preparing Toolbox…" : toolVerificationBusy ? "Checking model…" : saving ? "Starting…" : "Automate task"}</button></footer>
        </form>
      </div>,
      document.body,
    )}
  </>;
}

const terminalStatuses = new Set(["failed", "complete", "cancelled"]);

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
