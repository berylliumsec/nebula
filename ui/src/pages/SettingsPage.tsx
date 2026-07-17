import { useEffect, useState, type FormEvent } from "react";
import { Check, Contrast, KeyRound, Moon, Orbit, Pencil, Plus, RefreshCw, Server, Sun, Trash2, UserRound, X } from "lucide-react";
import type { LocalProviderDetection, OperatorProfile, ProviderCatalogEntry, ProviderHealth } from "../api/types";
import { useConfirmation } from "../components/DialogSystem";
import { PageHeader } from "../components/PageHeader";
import { ProviderHealthCard } from "../components/ProviderHealthCard";
import { ReleaseSettingsPanel } from "../components/ReleaseSettingsPanel";
import { EngagementPolicySettings } from "../components/EngagementPolicySettings";
import { AutomationRuntimeSettings, RunnerSettings } from "../components/ToolingSettings";
import { HarnessSettings } from "../components/HarnessSettings";
import { useTheme, type ThemePreference } from "../state/ThemeContext";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, DiagnosticsPanel, logCaughtDiagnostic } from "../diagnostics";

const themeOptions: { value: ThemePreference; label: string; icon: typeof Sun }[] = [
  { value: "system", label: "System", icon: Contrast },
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
  { value: "zero", label: "Zero", icon: Orbit },
  { value: "high-contrast", label: "High contrast", icon: Contrast },
];

const settingsSections = [
  ["setup-settings", "Setup", "Setup"],
  ["advanced-settings", "Advanced", "Advanced settings"],
  ["diagnostics-settings", "Diagnostics", "Diagnostics settings and recent errors"],
] as const;

type SettingsSection = typeof settingsSections[number][0];

function sectionFromHash(): SettingsSection {
  const hash = window.location.hash.slice(1);
  if (hash === "diagnostics-settings") return "diagnostics-settings";
  if (hash === "advanced-settings" || (hash && hash !== "setup-settings")) return "advanced-settings";
  return "setup-settings";
}

function providerOption(provider: ProviderHealth, key: string): string {
  const value = provider.options[key];
  return typeof value === "string" ? value : "";
}

function providerNumberOption(provider: ProviderHealth, key: string): string {
  const value = provider.options[key];
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? String(value) : "";
}

export function SettingsPage() {
  const confirm = useConfirmation();
  const { preference, setPreference } = useTheme();
  const {
    api,
    previewMode,
    health,
    providers,
    providerCatalog,
    refreshProvider,
    reverifyProvider,
    addProvider,
    updateProvider,
    setProviderEnabled,
    deleteProvider,
    operatorProfiles,
    createOperatorProfile,
    updateOperatorProfile,
    activateOperatorProfile,
    deleteOperatorProfile,
    refreshSetupRuntime,
    setupStatus,
    workspaceState,
  } = useWorkspace();
  const [adding, setAdding] = useState(false);
  const [editingProvider, setEditingProvider] = useState<ProviderHealth>();
  const [selected, setSelected] = useState<ProviderCatalogEntry>();
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [model, setModel] = useState("");
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [selectedModelIds, setSelectedModelIds] = useState<string[]>([]);
  const [credentialEnv, setCredentialEnv] = useState("");
  const [credentialSecret, setCredentialSecret] = useState("");
  const [sessionCredential, setSessionCredential] = useState(false);
  const [vertexProject, setVertexProject] = useState("");
  const [vertexLocation, setVertexLocation] = useState("");
  const [awsRegion, setAwsRegion] = useState("");
  const [contextWindow, setContextWindow] = useState("");
  const [maxOutputTokens, setMaxOutputTokens] = useState("");
  const [permitsSensitiveData, setPermitsSensitiveData] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string>();
  const [providerBusy, setProviderBusy] = useState<string>();
  const [providerActionError, setProviderActionError] = useState<string>();
  const [operatorDialog, setOperatorDialog] = useState(false);
  const [editingOperator, setEditingOperator] = useState<OperatorProfile>();
  const [operatorName, setOperatorName] = useState("");
  const [operatorEmail, setOperatorEmail] = useState("");
  const [operatorRole, setOperatorRole] = useState("");
  const [operatorBusy, setOperatorBusy] = useState<string>();
  const [operatorError, setOperatorError] = useState<string>();
  const [settingsSection, setSettingsSection] = useState<SettingsSection>(sectionFromHash);
  const [checkingTerminal, setCheckingTerminal] = useState(false);
  const [selectingRuntime, setSelectingRuntime] = useState<string>();
  const [setupError, setSetupError] = useState<string>();
  const [detectedLocalProviders, setDetectedLocalProviders] = useState<LocalProviderDetection[]>([]);
  const [detectingLocalProviders, setDetectingLocalProviders] = useState(false);

  useEffect(() => {
    if (!api || !["ready", "degraded"].includes(workspaceState)) {
      setDetectedLocalProviders([]);
      setDetectingLocalProviders(false);
      return;
    }
    const controller = new AbortController();
    setDetectingLocalProviders(true);
    void api.discoverLocalProviders(controller.signal)
      .then(setDetectedLocalProviders)
      .catch((caughtError) => {
        void logCaughtDiagnostic("interface.settings_page.caught_failure_01", "A handled interface operation failed.", caughtError, "settings_page");
        if (!controller.signal.aborted) setDetectedLocalProviders([]);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetectingLocalProviders(false);
      });
    return () => controller.abort();
  }, [api, workspaceState]);

  useEffect(() => {
    const syncSection = () => setSettingsSection(sectionFromHash());
    window.addEventListener("hashchange", syncSection);
    return () => window.removeEventListener("hashchange", syncSection);
  }, []);

  useEffect(() => {
    if (!adding || !editingProvider) return;
    const current = providers.find((provider) => provider.id === editingProvider.id);
    if (!current) return;
    setAvailableModels((models) => [...new Set([
      ...models,
      ...(current.availableModels ?? []),
      ...current.models,
      ...current.modelAllowlist,
      ...(current.defaultModel ? [current.defaultModel] : []),
    ])]);
  }, [adding, editingProvider, providers]);

  const openProviderDialog = () => {
    const entry = providerCatalog.find((item) => item.flavor === "vllm") ?? providerCatalog[0];
    if (!entry) return;
    setEditingProvider(undefined);
    setSelected(entry);
    setName(entry.flavor === "vllm" ? "Local vLLM" : entry.displayName);
    setEndpoint(entry.defaultBaseUrl ?? "");
    setModel("");
    setAvailableModels([]);
    setSelectedModelIds([]);
    setCredentialEnv(entry.suggestedKeyEnv ?? "");
    setCredentialSecret("");
    setSessionCredential(false);
    setVertexProject("");
    setVertexLocation("");
    setAwsRegion("");
    setContextWindow("");
    setMaxOutputTokens("");
    setPermitsSensitiveData(false);
    setProviderActionError(undefined);
    setFormError(undefined);
    setAdding(true);
  };

  const openDetectedProvider = (detected: LocalProviderDetection) => {
    const entry = providerCatalog.find((item) => item.flavor === detected.flavor);
    if (!entry) return;
    setEditingProvider(undefined);
    setSelected(entry);
    setName(detected.displayName);
    setEndpoint(detected.endpoint);
    setModel(detected.models[0] ?? "");
    setAvailableModels(detected.models);
    setSelectedModelIds([]);
    setCredentialEnv("");
    setCredentialSecret("");
    setSessionCredential(false);
    setVertexProject("");
    setVertexLocation("");
    setAwsRegion("");
    setContextWindow("");
    setMaxOutputTokens("");
    setPermitsSensitiveData(false);
    setProviderActionError(undefined);
    setFormError(undefined);
    setAdding(true);
  };

  const openProviderEdit = (provider: ProviderHealth) => {
    const entry = providerCatalog.find((item) => item.flavor === provider.providerType);
    setEditingProvider(provider);
    setSelected(entry);
    setName(provider.name);
    setEndpoint(provider.endpoint ?? entry?.defaultBaseUrl ?? "");
    setModel(provider.defaultModel ?? "");
    setAvailableModels([...new Set([
      ...(provider.availableModels ?? []),
      ...provider.models,
      ...provider.modelAllowlist,
      ...(provider.defaultModel ? [provider.defaultModel] : []),
    ])]);
    setSelectedModelIds(provider.modelAllowlist);
    setCredentialEnv(provider.credentialEnv ?? "");
    setCredentialSecret("");
    setSessionCredential(provider.credentialRef?.startsWith("session:") ?? false);
    setVertexProject(providerOption(provider, "project"));
    setVertexLocation(providerOption(provider, "location"));
    setAwsRegion(providerOption(provider, "region"));
    setContextWindow(providerNumberOption(provider, "context_window"));
    setMaxOutputTokens(providerNumberOption(provider, "max_output_tokens"));
    setPermitsSensitiveData(provider.permitsSensitiveData);
    setProviderActionError(undefined);
    setFormError(undefined);
    setAdding(true);
    void refreshProvider(provider.id);
  };

  const chooseProvider = (flavor: string) => {
    const entry = providerCatalog.find((item) => item.flavor === flavor);
    if (!entry) return;
    setSelected(entry);
    setName(entry.displayName);
    setEndpoint(entry.defaultBaseUrl ?? "");
    setModel("");
    setAvailableModels([]);
    setSelectedModelIds([]);
    setCredentialEnv(entry.suggestedKeyEnv ?? "");
    setCredentialSecret("");
    setSessionCredential(false);
    setVertexProject("");
    setVertexLocation("");
    setAwsRegion("");
    setContextWindow("");
    setMaxOutputTokens("");
    setPermitsSensitiveData(false);
  };

  const submitProvider = async (event: FormEvent) => {
    event.preventDefault();
    const providerType = editingProvider?.providerType ?? selected?.flavor;
    if (!providerType) return;
    if (!name.trim()) {
      setFormError("A provider profile name is required.");
      return;
    }
    if (providerType === "vertex" && (!vertexProject.trim() || !vertexLocation.trim())) {
      setFormError("Vertex profiles require a Google Cloud project and location.");
      return;
    }
    const modelAllowlist = selectedModelIds;
    const options = { ...(editingProvider?.options ?? {}) };
    const parsedContextWindow = contextWindow ? Number(contextWindow) : undefined;
    const parsedMaxOutputTokens = maxOutputTokens ? Number(maxOutputTokens) : undefined;
    if ((parsedContextWindow !== undefined && (!Number.isInteger(parsedContextWindow) || parsedContextWindow < 1))
      || (parsedMaxOutputTokens !== undefined && (!Number.isInteger(parsedMaxOutputTokens) || parsedMaxOutputTokens < 1))) {
      setFormError("Context window and maximum output tokens must be positive integers.");
      return;
    }
    if (parsedContextWindow !== undefined && parsedMaxOutputTokens !== undefined && parsedMaxOutputTokens >= parsedContextWindow) {
      setFormError("Maximum output tokens must be smaller than the context window.");
      return;
    }
    if (parsedContextWindow !== undefined) options.context_window = parsedContextWindow;
    else delete options.context_window;
    if (parsedMaxOutputTokens !== undefined) options.max_output_tokens = parsedMaxOutputTokens;
    else delete options.max_output_tokens;
    if (providerType === "vertex") {
      options.project = vertexProject.trim();
      options.location = vertexLocation.trim();
    }
    if (providerType === "bedrock") {
      if (awsRegion.trim()) options.region = awsRegion.trim();
      else delete options.region;
    }
    setSaving(true);
    setFormError(undefined);
    let createdCredentialRef: string | undefined;
    try {
      if (credentialSecret) {
        if (!api) throw new Error("Nebula Core must be available to store a credential.");
        const credential = await api.createCredential(
          credentialSecret,
          sessionCredential ? "session" : "vault",
        );
        createdCredentialRef = credential.reference;
      }
      const credentialRef = createdCredentialRef
        ?? (credentialEnv ? undefined : editingProvider?.credentialRef)
        ?? undefined;
      if (editingProvider) {
        await updateProvider(editingProvider.id, {
          name,
          providerType,
          endpoint: endpoint || undefined,
          local: editingProvider.local,
          defaultModel: model || undefined,
          modelAllowlist,
          credentialEnv: credentialRef ? undefined : credentialEnv || undefined,
          credentialRef,
          permitsSensitiveData,
          retention: editingProvider.retention,
          residency: editingProvider.residency,
          options,
          metadata: editingProvider.metadata,
          expectedRevision: editingProvider.revision,
        });
      } else if (selected) {
        await addProvider({
          name,
          providerType,
          endpoint: endpoint || undefined,
          local: selected.local,
          defaultModel: model || undefined,
          modelAllowlist,
          credentialEnv: credentialRef ? undefined : credentialEnv || undefined,
          credentialRef,
          permitsSensitiveData,
          options,
        });
      }
      setAdding(false);
      setCredentialSecret("");
    } catch (error) {
      void logCaughtDiagnostic("interface.settings_page.caught_failure_02", "A handled interface operation failed.", error, "settings_page");
      if (createdCredentialRef && api) {
        await api.deleteCredential(createdCredentialRef).catch((caughtError) => { void logCaughtDiagnostic("interface.settings_page.caught_failure_03", "A handled interface operation failed.", caughtError, "settings_page"); return undefined; });
      }
      setFormError(error instanceof Error ? error.message : `Could not ${editingProvider ? "save" : "add"} provider.`);
    } finally {
      setSaving(false);
    }
  };

  const toggleProvider = async (provider: ProviderHealth) => {
    setProviderBusy(provider.id);
    setProviderActionError(undefined);
    try {
      await setProviderEnabled(provider.id, !provider.enabled, provider.revision);
    } catch (error) {
      void logCaughtDiagnostic("interface.settings_page.caught_failure_04", "A handled interface operation failed.", error, "settings_page");
      setProviderActionError(error instanceof Error ? error.message : "Could not change the provider state.");
    } finally {
      setProviderBusy(undefined);
    }
  };

  const removeProvider = async (provider: ProviderHealth) => {
    if (!await confirm({
      title: `Delete ${provider.name}?`,
      message: "This provider profile will be removed. Profiles referenced by chat or mission history remain protected by Core.",
      confirmLabel: "Delete provider",
      tone: "danger",
    })) return;
    setProviderBusy(provider.id);
    setProviderActionError(undefined);
    try {
      await deleteProvider(provider.id, provider.revision);
    } catch (error) {
      void logCaughtDiagnostic("interface.settings_page.caught_failure_05", "A handled interface operation failed.", error, "settings_page");
      setProviderActionError(error instanceof Error ? error.message : "Could not delete the provider profile.");
    } finally {
      setProviderBusy(undefined);
    }
  };

  const openOperator = (profile?: OperatorProfile) => {
    setEditingOperator(profile);
    setOperatorName(profile?.displayName ?? "");
    setOperatorEmail(profile?.email ?? "");
    setOperatorRole(profile?.role ?? "");
    setOperatorError(undefined);
    setOperatorDialog(true);
  };

  const submitOperator = async (event: FormEvent) => {
    event.preventDefault();
    setOperatorBusy(editingOperator?.id ?? "new");
    setOperatorError(undefined);
    try {
      if (editingOperator) {
        await updateOperatorProfile(editingOperator.id, { displayName: operatorName, email: operatorEmail, role: operatorRole, expectedRevision: editingOperator.revision });
      } else {
        await createOperatorProfile({ displayName: operatorName, email: operatorEmail || undefined, role: operatorRole || undefined });
      }
      setOperatorDialog(false);
    } catch (error) {
      void logCaughtDiagnostic("interface.settings_page.caught_failure_06", "A handled interface operation failed.", error, "settings_page");
      setOperatorError(error instanceof Error ? error.message : "Could not save the operator profile.");
    } finally {
      setOperatorBusy(undefined);
    }
  };

  const activateOperator = async (profile: OperatorProfile) => {
    setOperatorBusy(profile.id);
    setOperatorError(undefined);
    try {
      await activateOperatorProfile(profile.id, profile.revision);
    } catch (error) {
      void logCaughtDiagnostic("interface.settings_page.caught_failure_07", "A handled interface operation failed.", error, "settings_page");
      setOperatorError(error instanceof Error ? error.message : "Could not activate the operator profile.");
    } finally {
      setOperatorBusy(undefined);
    }
  };

  const removeOperator = async (profile: OperatorProfile) => {
    if (!await confirm({
      title: `Delete ${profile.displayName}?`,
      message: "The local attribution profile will be removed. Persisted evidence keeps its original attribution record.",
      confirmLabel: "Delete operator",
      tone: "danger",
    })) return;
    setOperatorBusy(profile.id);
    setOperatorError(undefined);
    try {
      await deleteOperatorProfile(profile.id, profile.revision);
    } catch (error) {
      void logCaughtDiagnostic("interface.settings_page.caught_failure_08", "A handled interface operation failed.", error, "settings_page");
      setOperatorError(error instanceof Error ? error.message : "Could not delete the operator profile.");
    } finally {
      setOperatorBusy(undefined);
    }
  };
  const checkTerminalSetup = async () => {
    setCheckingTerminal(true);
    setSetupError(undefined);
    try {
      await refreshSetupRuntime();
    } catch (error) {
      void logCaughtDiagnostic("interface.settings_page.caught_failure_09", "A handled interface operation failed.", error, "settings_page");
      setSetupError(error instanceof Error ? error.message : "Could not check Terminal setup.");
    } finally {
      setCheckingTerminal(false);
    }
  };
  const selectRuntime = async (candidateId: string) => {
    if (!api) return;
    setSelectingRuntime(candidateId);
    setSetupError(undefined);
    try {
      await api.selectSetupRuntime(candidateId);
      await refreshSetupRuntime();
    } catch (error) {
      void logCaughtDiagnostic("interface.settings_page.caught_failure_10", "A handled interface operation failed.", error, "settings_page");
      setSetupError(error instanceof Error ? error.message : "Could not select the container runtime.");
    } finally {
      setSelectingRuntime(undefined);
    }
  };
  const dialogProviderType = editingProvider?.providerType ?? selected?.flavor ?? "";
  const dialogLocal = editingProvider?.local ?? selected?.local ?? false;
  const dialogModels = [...new Set([
    ...availableModels,
    ...selectedModelIds,
    ...(model ? [model] : []),
  ])];
  const unconfiguredDetectedProviders = detectedLocalProviders.filter((detected) =>
    !providers.some((provider) => provider.endpoint === detected.endpoint && provider.enabled));
  const primaryProviderFlavors = ["openai", "anthropic", "gemini"];
  const primaryProviderCatalog = primaryProviderFlavors
    .map((flavor) => providerCatalog.find((entry) => entry.flavor === flavor))
    .filter((entry): entry is ProviderCatalogEntry => Boolean(entry));
  const moreProviderCatalog = providerCatalog.filter(
    (entry) => !primaryProviderFlavors.includes(entry.flavor),
  );
  return (
    <div className="page settings-page">
      <PageHeader title="Settings" description="Workspace preferences." />
      <div className="settings-workspace">
      <nav className="settings-tabs" aria-label="Settings sections">{settingsSections.map(([id, label, accessibleLabel]) => <a className={settingsSection === id ? "active" : undefined} aria-label={accessibleLabel} aria-current={settingsSection === id ? "page" : undefined} href={`#${id}`} key={id} onClick={(event) => { event.preventDefault(); window.history.replaceState(null, "", `#${id}`); setSettingsSection(id); }}>{label}</a>)}</nav>
      <div className="settings-detail" data-section={settingsSection}>
      <section className="settings-section setup-overview" id="setup-settings">
        <div className="section-heading"><div><h2>Ready to work</h2><p>Models are optional.</p></div></div>
        <div className="setup-card-grid">
          <article className="panel setup-card">
            <header><span className={`status-dot ${setupStatus?.terminal.status === "ready" ? "healthy" : ["detecting_runner", "preparing_image"].includes(setupStatus?.terminal.status ?? "") ? "warning" : "unavailable"}`} /><div><small>Terminal</small><h3>{setupStatus?.terminal.status === "ready" ? "Ready" : setupStatus?.terminal.status === "detecting_runner" ? "Checking your runtime…" : setupStatus?.terminal.status === "preparing_image" ? "Preparing workstation…" : setupStatus?.terminal.status === "needs_runner" ? "Docker or Podman needed" : "Needs attention"}</h3></div></header>
            <p>{setupStatus?.terminal.detail ?? (setupStatus?.terminal.status === "ready" ? "A verified local container runtime is ready for project terminals." : "Nebula checks trusted local Docker and Podman installations automatically.")}</p>
            {setupStatus?.terminal.candidates.length ? <div className="setup-runtime-options" aria-label="Local container runtime choices">{setupStatus.terminal.candidates.map((candidate) => <article key={`${candidate.runtime}-${candidate.executable}`}><span><strong>{candidate.name}</strong><small>{candidate.runtime} · {candidate.platform} · {candidate.healthy ? "ready" : candidate.detail ?? "unavailable"}</small></span>{candidate.candidateId && candidate.healthy && candidate.runnerProfileId !== setupStatus.terminal.runnerProfileId ? <button className="button quiet" type="button" disabled={Boolean(selectingRuntime)} onClick={() => candidate.candidateId && void selectRuntime(candidate.candidateId)}>{selectingRuntime === candidate.candidateId ? "Selecting…" : "Use"}</button> : candidate.runnerProfileId === setupStatus.terminal.runnerProfileId ? <small><Check size={13} /> Selected</small> : null}</article>)}</div> : null}
            <button className="button secondary" type="button" disabled={checkingTerminal || workspaceState === "starting" || workspaceState === "failed"} onClick={() => void checkTerminalSetup()}><RefreshCw className={checkingTerminal ? "spin" : undefined} size={15} /> {checkingTerminal ? "Checking…" : "Check again"}</button>
          </article>
          <article className="panel setup-card">
            <header><span className={`status-dot ${setupStatus?.assistant.status === "configured" || providers.length ? "healthy" : "warning"}`} /><div><small>Assistant · optional</small><h3>{setupStatus?.assistant.status === "configured" || providers.length ? "Model connected" : "No model connected"}</h3></div></header>
            <p>{setupStatus?.assistant.detail ?? (providers.length ? `${providers.length} model provider${providers.length === 1 ? " is" : "s are"} configured.` : "You can use Terminal and Files now, then connect a model whenever you need the assistant.")}</p>
            {detectingLocalProviders && !detectedLocalProviders.length && <p className="setup-footnote"><RefreshCw className="spin" size={14} /> Checking Ollama, vLLM, and LM Studio…</p>}
            {unconfiguredDetectedProviders.length > 0 && <div className="setup-detected-providers" aria-label="Detected local model services">{unconfiguredDetectedProviders.map((detected) => <button className="button secondary" type="button" key={`${detected.flavor}-${detected.endpoint}`} onClick={() => openDetectedProvider(detected)}><Server size={14} /> Use {detected.displayName}{detected.models[0] ? ` · ${detected.models[0]}` : ""}</button>)}</div>}
            <button className="button secondary" type="button" onClick={() => { window.history.replaceState(null, "", "#advanced-settings"); setSettingsSection("advanced-settings"); }}>{providers.length ? "Manage models" : unconfiguredDetectedProviders.length ? "More providers" : "Connect a model"}</button>
          </article>
        </div>
        {setupStatus?.scratchProjectId && <p className="setup-footnote"><Check size={14} /> Scratch Project is ready.</p>}
        {setupError && <DiagnosticErrorNotice error={setupError} fallback="Setup could not complete the operation." />}
        {!setupStatus && workspaceState !== "starting" && <p className="setup-footnote">Setup details are unavailable. Core reports runner status: {health?.runner ?? "unknown"}.</p>}
      </section>
      <DiagnosticsPanel hidden={settingsSection !== "diagnostics-settings"} />
      <section className="settings-section" id="provider-settings">
        <div className="section-heading"><div><h2>Model providers</h2><p>Models and capabilities</p></div><button className="button primary" type="button" disabled={previewMode || providerCatalog.length === 0} onClick={openProviderDialog}><Plus size={16} /> Add provider</button></div>
        {providerActionError && <DiagnosticErrorNotice error={providerActionError} fallback="The provider operation could not be completed." />}
        {providers.length > 0 ? (
          <div className="provider-grid">{providers.map((provider) => <ProviderHealthCard provider={provider} preview={previewMode} busy={providerBusy === provider.id} onRefresh={refreshProvider} onReverify={reverifyProvider} onEdit={openProviderEdit} onToggle={toggleProvider} onDelete={removeProvider} key={provider.id} />)}</div>
        ) : (
          <div className="empty-state compact"><Server size={23} /><strong>No provider profiles</strong><p>Add a provider profile in Core before assigning a model to a mission.</p></div>
        )}
      </section>
      <HarnessSettings />
      <AutomationRuntimeSettings />
      <RunnerSettings />
      <EngagementPolicySettings />
      <section className="settings-section" id="operator-settings">
        <div className="section-heading"><div><h2>Operator profiles</h2><p>Local activity attribution</p></div><button className="button primary" type="button" disabled={previewMode} onClick={() => openOperator()}><Plus size={16} /> Add operator</button></div>
        {operatorError && <DiagnosticErrorNotice error={operatorError} fallback="The operator profile operation could not be completed." />}
        {operatorProfiles.length ? <div className="operator-profile-list">{operatorProfiles.map((profile) => <article className={profile.active ? "active" : undefined} key={profile.id}><span className="operator-profile-avatar">{profile.displayName.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "OP"}</span><div><h3 title={profile.displayName}>{profile.displayName}</h3><p title={`${profile.role || "Local operator"}${profile.email ? ` · ${profile.email}` : ""}`}>{profile.role || "Local operator"}{profile.email ? ` · ${profile.email}` : ""}</p></div>{profile.active ? <span className="operator-active"><Check size={13} /> Active</span> : <button className="button quiet" type="button" disabled={operatorBusy === profile.id} onClick={() => void activateOperator(profile)}>Activate</button>}<button className="icon-button subtle" type="button" aria-label={`Edit ${profile.displayName}`} disabled={operatorBusy === profile.id} onClick={() => openOperator(profile)}><Pencil size={14} /></button><button className="icon-button subtle" type="button" aria-label={`Delete ${profile.displayName}`} title={profile.active ? "Activate another profile before deleting this one" : operatorProfiles.length <= 1 ? "The last operator profile cannot be deleted" : "Delete operator profile"} disabled={operatorBusy === profile.id || profile.active || operatorProfiles.length <= 1} onClick={() => void removeOperator(profile)}><Trash2 size={14} /></button></article>)}</div> : <div className="empty-state compact"><UserRound size={23} /><strong>No durable operator profile</strong><p>Create a local profile so new evidence has explicit attribution and the workspace can show who is active.</p></div>}
      </section>
      <div className="settings-bottom-grid" id="general-settings">
        <section className="panel appearance-panel" id="appearance-settings">
          <header className="panel-header compact"><div><h2>Appearance</h2><p>Saved on this device</p></div><Contrast size={19} /></header>
          <div className="theme-options">
            {themeOptions.map(({ value, label, icon: Icon }) => (
              <button key={value} type="button" aria-pressed={preference === value} onClick={() => setPreference(value)}><span><Icon size={18} /></span><strong>{label}</strong>{preference === value && <small>Active</small>}</button>
            ))}
          </div>
          <p className="appearance-help">Choose a theme for this device.</p>
        </section>
        <section className="panel secrets-panel" id="security-settings">
          <header className="panel-header compact"><div><h2>Credential references</h2><p>Secrets never enter agent context</p></div><KeyRound size={19} /></header>
          <div className="empty-state compact"><KeyRound size={23} /><strong>Write-only</strong><p>Secrets stay in the system vault.</p></div>
        </section>
        <ReleaseSettingsPanel />
      </div>
      </div>
      </div>
      {adding && (selected || editingProvider) && (
        <div className="dialog-backdrop">
          <form className="provider-dialog" role="dialog" aria-modal="true" aria-labelledby="provider-dialog-title" onSubmit={(event) => void submitProvider(event)}>
            <header><div><small>Provider profile</small><h2 id="provider-dialog-title">{editingProvider ? `Edit ${editingProvider.name}` : "Add model provider"}</h2></div><button className="icon-button subtle" type="button" aria-label="Close provider dialog" onClick={() => setAdding(false)}><X size={17} /></button></header>
            <label>Provider type<select value={dialogProviderType} disabled={Boolean(editingProvider)} onChange={(event) => chooseProvider(event.target.value)}>{!providerCatalog.some((entry) => entry.flavor === dialogProviderType) && <option value={dialogProviderType}>{dialogProviderType}</option>}<optgroup label="Recommended cloud providers">{primaryProviderCatalog.map((entry) => <option value={entry.flavor} key={entry.flavor}>{entry.displayName}</option>)}</optgroup><optgroup label="More providers">{moreProviderCatalog.map((entry) => <option value={entry.flavor} key={entry.flavor}>{entry.displayName}</option>)}</optgroup></select></label>
            {editingProvider && <p className="provider-dialog-note">Provider type and locality are fixed after creation. Other profile settings use revision-safe updates.</p>}
            <label>Profile name<input required value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label>Endpoint<input required={!selected?.defaultBaseUrl} value={endpoint} placeholder={selected?.defaultBaseUrl ?? "https://provider.example/v1"} onChange={(event) => setEndpoint(event.target.value)} /></label>
            <label>Default model<select aria-describedby="provider-model-help" value={model} disabled={!dialogModels.length} onChange={(event) => setModel(event.target.value)}><option value="">{dialogModels.length ? "Automatic (first available model)" : "Discovered after saving"}</option>{dialogModels.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
            <p className="provider-dialog-note" id="provider-model-help">{dialogModels.length ? "Choose a model reported by this runtime, or leave automatic selection enabled." : "Save the profile to run model discovery. Then edit it to choose a default from the reported models."}</p>
            <label>Credential<input type="password" autoComplete="new-password" value={credentialSecret} placeholder={editingProvider?.credentialRef || editingProvider?.credentialEnv ? "Leave blank to keep the current credential" : dialogLocal ? "Optional for local services" : "API key or token"} onChange={(event) => setCredentialSecret(event.target.value)} /></label>
            {credentialSecret && <label className="provider-consent"><input type="checkbox" checked={sessionCredential} onChange={(event) => setSessionCredential(event.target.checked)} /><span><strong>Use for this Nebula session only</strong><small>When off, Core saves the secret in the operating-system credential vault. It is never returned or stored in the database.</small></span></label>}
            <details className="provider-advanced"><summary>Advanced provider options</summary>
              <fieldset className="resource-checklist"><legend>Allowed models</legend>{dialogModels.length ? dialogModels.map((item) => <label key={item}><input type="checkbox" checked={selectedModelIds.includes(item)} onChange={(event) => setSelectedModelIds((current) => event.target.checked ? [...new Set([...current, item])] : current.filter((value) => value !== item))} /><span>{item}</span></label>) : <p>Models will appear after the provider health check.</p>}</fieldset>
              <p className="provider-dialog-note">Leave every model unchecked to allow all models reported by the provider. With restrictions enabled, the default is included automatically.</p>
              <div className="resource-form-grid"><label>Context window (tokens)<input type="number" min="1" inputMode="numeric" value={contextWindow} placeholder="8192 safe fallback" onChange={(event) => setContextWindow(event.target.value)} /></label><label>Maximum output tokens<input type="number" min="1" inputMode="numeric" value={maxOutputTokens} placeholder="2048 default" onChange={(event) => setMaxOutputTokens(event.target.value)} /></label></div>
              {dialogProviderType === "vertex" && <div className="resource-form-grid"><label>Google Cloud project<input required value={vertexProject} placeholder="my-security-project" onChange={(event) => setVertexProject(event.target.value)} /></label><label>Vertex location<input required value={vertexLocation} placeholder="us-central1" onChange={(event) => setVertexLocation(event.target.value)} /></label></div>}
              {dialogProviderType === "bedrock" && <label>AWS region<input value={awsRegion} placeholder="Uses the ambient AWS region when blank" onChange={(event) => setAwsRegion(event.target.value)} /></label>}
              <label>Credential environment variable<input value={credentialEnv} pattern="[A-Za-z_][A-Za-z0-9_]*" placeholder={dialogLocal ? "Optional for authenticated local gateways" : "For example, OPENAI_API_KEY"} autoCapitalize="none" spellCheck={false} onChange={(event) => setCredentialEnv(event.target.value)} /></label>
              {!dialogLocal && <label className="provider-consent"><input type="checkbox" checked={permitsSensitiveData} onChange={(event) => setPermitsSensitiveData(event.target.checked)} /><span><strong>Allow project and document data</strong><small>Permit redacted excerpts only after confirmation for each cloud request.</small></span></label>}
            </details>
            <p className="provider-dialog-note">{dialogLocal ? "Local-only profile. Nebula will not route it to a cloud fallback." : credentialSecret ? sessionCredential ? "The credential will remain only in Core memory for this session." : "The credential will be stored in the operating-system vault; only an opaque reference is saved." : credentialEnv ? `Core will resolve env:${credentialEnv}; the secret value is never saved in this profile.` : editingProvider?.credentialRef ? "The current write-only credential reference will be retained." : "Ambient provider credentials remain available for supported services."}</p>
            {selected?.notes && <p className="provider-dialog-note">{selected.notes}</p>}
            <p className="provider-dialog-note">Saving performs only liveness and model discovery. Tool calling is verified later, when you enable automation.</p>
            {formError && <DiagnosticErrorNotice error={formError} fallback="The form could not be saved." compact />}
            <footer><button className="button secondary" type="button" onClick={() => setAdding(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving || !name.trim() || (dialogProviderType === "vertex" && (!vertexProject.trim() || !vertexLocation.trim()))}>{saving ? "Saving…" : editingProvider ? "Save provider" : "Add provider"}</button></footer>
          </form>
        </div>
      )}
      {operatorDialog && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="operator-dialog-title" onSubmit={(event) => void submitOperator(event)}><header><div><small>Local attribution</small><h2 id="operator-dialog-title">{editingOperator ? "Edit operator" : "Add operator"}</h2></div><button className="icon-button subtle" type="button" aria-label="Close operator dialog" onClick={() => setOperatorDialog(false)}><X size={17} /></button></header><label>Display name<input required autoFocus value={operatorName} onChange={(event) => setOperatorName(event.target.value)} /></label><label>Email<input type="email" value={operatorEmail} onChange={(event) => setOperatorEmail(event.target.value)} /></label><label>Role<input value={operatorRole} placeholder="Project lead, analyst…" onChange={(event) => setOperatorRole(event.target.value)} /></label><p className="provider-dialog-note">This identity is stored locally for attribution only. It is not an authentication account and grants no permissions.</p>{operatorError && <DiagnosticErrorNotice error={operatorError} fallback="The operator profile could not be saved." compact />}<footer><button className="button secondary" type="button" onClick={() => setOperatorDialog(false)}>Cancel</button><button className="button primary" type="submit" disabled={Boolean(operatorBusy)}>{operatorBusy ? "Saving…" : "Save operator"}</button></footer></form></div>}
    </div>
  );
}
