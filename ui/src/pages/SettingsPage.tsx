import { useEffect, useState, type FormEvent } from "react";
import { Check, Contrast, Download, KeyRound, Moon, PackageCheck, Pencil, Plus, Server, Sun, Trash2, UserRound, X } from "lucide-react";
import type { OperatorProfile, ProviderCatalogEntry, ProviderHealth } from "../api/types";
import {
  checkForUpdate,
  getReleaseInfo,
  installAvailableUpdate,
  type AvailableUpdate,
  type ReleaseInfo,
} from "../api/updater";
import { useConfirmation } from "../components/DialogSystem";
import { PageHeader } from "../components/PageHeader";
import { ProviderHealthCard } from "../components/ProviderHealthCard";
import { EngagementPolicySettings } from "../components/EngagementPolicySettings";
import { RunnerSettings, ToolPackSettings } from "../components/ToolingSettings";
import { useTheme, type ThemePreference } from "../state/ThemeContext";
import { useWorkspace } from "../state/WorkspaceContext";

const themeOptions: { value: ThemePreference; label: string; icon: typeof Sun }[] = [
  { value: "system", label: "System", icon: Contrast },
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
  { value: "high-contrast", label: "High contrast", icon: Contrast },
];

const settingsSections = [
  ["general-settings", "General"],
  ["provider-settings", "AI Providers"],
  ["tool-pack-settings", "Toolbox"],
  ["runtime-settings", "Runners"],
  ["engagement-policy-settings", "Engagement Policy"],
  ["operator-settings", "Operators"],
  ["security-settings", "Privacy & Security"],
] as const;

type SettingsSection = typeof settingsSections[number][0];

function sectionFromHash(): SettingsSection {
  const hash = window.location.hash.slice(1);
  if (hash === "appearance-settings") return "general-settings";
  return settingsSections.some(([id]) => id === hash) ? hash as SettingsSection : "general-settings";
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
    previewMode,
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
  } = useWorkspace();
  const [adding, setAdding] = useState(false);
  const [editingProvider, setEditingProvider] = useState<ProviderHealth>();
  const [selected, setSelected] = useState<ProviderCatalogEntry>();
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [model, setModel] = useState("");
  const [modelAllowlistText, setModelAllowlistText] = useState("");
  const [credentialEnv, setCredentialEnv] = useState("");
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
  const [release, setRelease] = useState<ReleaseInfo>();
  const [availableUpdate, setAvailableUpdate] = useState<AvailableUpdate>();
  const [updateState, setUpdateState] = useState<"idle" | "checking" | "installing" | "current" | "restart" | "error">("idle");
  const [updateMessage, setUpdateMessage] = useState<string>();

  useEffect(() => {
    let active = true;
    void getReleaseInfo()
      .then((info) => {
        if (active) setRelease(info);
      })
      .catch(() => {
        if (active) setRelease(undefined);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const syncSection = () => setSettingsSection(sectionFromHash());
    window.addEventListener("hashchange", syncSection);
    return () => window.removeEventListener("hashchange", syncSection);
  }, []);

  const checkUpdates = async () => {
    setUpdateState("checking");
    setUpdateMessage(undefined);
    try {
      const update = await checkForUpdate();
      setAvailableUpdate(update);
      setUpdateState(update ? "idle" : "current");
    } catch (error) {
      setUpdateState("error");
      setUpdateMessage(error instanceof Error ? error.message : "Could not check for updates.");
    }
  };

  const installUpdate = async () => {
    setUpdateState("installing");
    setUpdateMessage(undefined);
    try {
      const installed = await installAvailableUpdate();
      setUpdateState(installed ? "restart" : "current");
      if (!installed) setAvailableUpdate(undefined);
    } catch (error) {
      setUpdateState("error");
      setUpdateMessage(error instanceof Error ? error.message : "Could not install the update.");
    }
  };

  const openProviderDialog = () => {
    const entry = providerCatalog.find((item) => item.flavor === "vllm") ?? providerCatalog[0];
    if (!entry) return;
    setEditingProvider(undefined);
    setSelected(entry);
    setName(entry.flavor === "vllm" ? "Local vLLM" : entry.displayName);
    setEndpoint(entry.defaultBaseUrl ?? "");
    setModel("");
    setModelAllowlistText("");
    setCredentialEnv(entry.suggestedKeyEnv ?? "");
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
    setModelAllowlistText(provider.modelAllowlist.join("\n"));
    setCredentialEnv(provider.credentialEnv ?? "");
    setVertexProject(providerOption(provider, "project"));
    setVertexLocation(providerOption(provider, "location"));
    setAwsRegion(providerOption(provider, "region"));
    setContextWindow(providerNumberOption(provider, "context_window"));
    setMaxOutputTokens(providerNumberOption(provider, "max_output_tokens"));
    setPermitsSensitiveData(provider.permitsSensitiveData);
    setProviderActionError(undefined);
    setFormError(undefined);
    setAdding(true);
  };

  const chooseProvider = (flavor: string) => {
    const entry = providerCatalog.find((item) => item.flavor === flavor);
    if (!entry) return;
    setSelected(entry);
    setName(entry.displayName);
    setEndpoint(entry.defaultBaseUrl ?? "");
    setModel("");
    setModelAllowlistText("");
    setCredentialEnv(entry.suggestedKeyEnv ?? "");
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
    if (["anthropic", "bedrock"].includes(providerType) && !model.trim()) {
      setFormError(`${providerType === "bedrock" ? "AWS Bedrock" : "Anthropic"} profiles require a default model ID before chat or missions can use them.`);
      return;
    }
    if (providerType === "vertex" && (!vertexProject.trim() || !vertexLocation.trim())) {
      setFormError("Vertex profiles require a Google Cloud project and location.");
      return;
    }
    const modelAllowlist = [...new Set(modelAllowlistText.split(/[\n,]+/).map((value) => value.trim()).filter(Boolean))];
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
    try {
      if (editingProvider) {
        await updateProvider(editingProvider.id, {
          name,
          providerType,
          endpoint: endpoint || undefined,
          local: editingProvider.local,
          defaultModel: model || undefined,
          modelAllowlist,
          credentialEnv: credentialEnv || undefined,
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
          credentialEnv: credentialEnv || undefined,
          permitsSensitiveData,
          options,
        });
      }
      setAdding(false);
    } catch (error) {
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
      setOperatorError(error instanceof Error ? error.message : "Could not delete the operator profile.");
    } finally {
      setOperatorBusy(undefined);
    }
  };
  const dialogProviderType = editingProvider?.providerType ?? selected?.flavor ?? "";
  const dialogLocal = editingProvider?.local ?? selected?.local ?? false;
  const requiresDefaultModel = ["anthropic", "bedrock"].includes(dialogProviderType);
  const dialogAllowlist = [...new Set(modelAllowlistText.split(/[\n,]+/).map((value) => value.trim()).filter(Boolean))];
  return (
    <div className="page settings-page">
      <PageHeader eyebrow="Workspace configuration" title="Settings" description="Provider, sandbox runner, Toolbox environment, engagement policy, attribution, and privacy controls." />
      <div className="settings-workspace">
      <nav className="settings-tabs" aria-label="Settings sections">{settingsSections.map(([id, label]) => <a className={settingsSection === id ? "active" : undefined} aria-current={settingsSection === id ? "page" : undefined} href={`#${id}`} key={id} onClick={() => setSettingsSection(id)}>{label}</a>)}</nav>
      <div className="settings-detail" data-section={settingsSection}>
      <section className="settings-section" id="provider-settings">
        <div className="section-heading"><div><h2>Model providers</h2><p>Configured provider profiles and their declared capabilities.</p></div><button className="button primary" type="button" disabled={previewMode || providerCatalog.length === 0} onClick={openProviderDialog}><Plus size={16} /> Add provider</button></div>
        {providerActionError && <div className="knowledge-status error" role="alert">{providerActionError}</div>}
        {providers.length > 0 ? (
          <div className="provider-grid">{providers.map((provider) => <ProviderHealthCard provider={provider} preview={previewMode} busy={providerBusy === provider.id} onRefresh={refreshProvider} onReverify={reverifyProvider} onEdit={openProviderEdit} onToggle={toggleProvider} onDelete={removeProvider} key={provider.id} />)}</div>
        ) : (
          <div className="empty-state compact"><Server size={23} /><strong>No provider profiles</strong><p>Add a provider profile in Core before assigning a model to a mission.</p></div>
        )}
      </section>
      <ToolPackSettings />
      <RunnerSettings />
      <EngagementPolicySettings />
      <section className="settings-section" id="operator-settings">
        <div className="section-heading"><div><h2>Local operator profiles</h2><p>Durable attribution for local activity. Profiles do not grant authentication or RBAC permissions.</p></div><button className="button primary" type="button" disabled={previewMode} onClick={() => openOperator()}><Plus size={16} /> Add operator</button></div>
        {operatorError && <div className="knowledge-status error" role="alert">{operatorError}</div>}
        {operatorProfiles.length ? <div className="operator-profile-list">{operatorProfiles.map((profile) => <article className={profile.active ? "active" : undefined} key={profile.id}><span className="operator-profile-avatar">{profile.displayName.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "OP"}</span><div><h3>{profile.displayName}</h3><p>{profile.role || "Local operator"}{profile.email ? ` · ${profile.email}` : ""}</p></div>{profile.active ? <span className="operator-active"><Check size={13} /> Active</span> : <button className="button quiet" type="button" disabled={operatorBusy === profile.id} onClick={() => void activateOperator(profile)}>Activate</button>}<button className="icon-button subtle" type="button" aria-label={`Edit ${profile.displayName}`} disabled={operatorBusy === profile.id} onClick={() => openOperator(profile)}><Pencil size={14} /></button><button className="icon-button subtle" type="button" aria-label={`Delete ${profile.displayName}`} title={profile.active ? "Activate another profile before deleting this one" : operatorProfiles.length <= 1 ? "The last operator profile cannot be deleted" : "Delete operator profile"} disabled={operatorBusy === profile.id || profile.active || operatorProfiles.length <= 1} onClick={() => void removeOperator(profile)}><Trash2 size={14} /></button></article>)}</div> : <div className="empty-state compact"><UserRound size={23} /><strong>No durable operator profile</strong><p>Create a local profile so new evidence has explicit attribution and the workspace can show who is active.</p></div>}
      </section>
      <div className="settings-bottom-grid" id="general-settings">
        <section className="panel appearance-panel" id="appearance-settings">
          <header className="panel-header compact"><div><h2>Appearance</h2><p>Saved on this device</p></div><Contrast size={19} /></header>
          <div className="theme-options">
            {themeOptions.map(({ value, label, icon: Icon }) => (
              <button key={value} type="button" aria-pressed={preference === value} onClick={() => setPreference(value)}><span><Icon size={18} /></span><strong>{label}</strong>{preference === value && <small>Active</small>}</button>
            ))}
          </div>
          <p className="appearance-help">All themes preserve visible keyboard focus. High contrast strengthens boundaries and meets system forced-color expectations.</p>
        </section>
        <section className="panel secrets-panel" id="security-settings">
          <header className="panel-header compact"><div><h2>Credential references</h2><p>Secrets never enter agent context</p></div><KeyRound size={19} /></header>
          <div className="empty-state compact"><KeyRound size={23} /><strong>Managed outside prompts</strong><p>Set provider secrets in Core’s environment before launch, then save only the environment variable name in a provider profile. An in-app secret store is not available in this preview.</p></div>
        </section>
        <section className="panel release-panel">
          <header className="panel-header compact"><div><h2>About Nebula</h2><p>Build and update channel</p></div><PackageCheck size={19} /></header>
          <dl>
            <div><dt>Desktop version</dt><dd>{release?.version ?? "Detecting…"}</dd></div>
            <div><dt>Distribution</dt><dd>{release?.distribution ?? "Unknown"}</dd></div>
            <div><dt>Build</dt><dd title={release?.commit}>{release?.commit.slice(0, 12) ?? "Unknown"}</dd></div>
            <div><dt>Target</dt><dd>{release?.buildTarget ?? "Unknown"}</dd></div>
            <div><dt>Built</dt><dd>{release?.builtAt ?? "Unknown"}</dd></div>
            {release?.updateChannel && <div><dt>Update channel</dt><dd>{release.updateChannel}</dd></div>}
          </dl>
          <div className="release-actions">
            {release?.updaterEnabled ? (
              availableUpdate ? (
                <button className="button primary full" type="button" disabled={updateState === "installing"} onClick={() => void installUpdate()}>
                  <Download size={15} /> {updateState === "installing" ? "Installing…" : `Install ${availableUpdate.version}`}
                </button>
              ) : (
                <button className="button secondary full" type="button" disabled={updateState === "checking" || updateState === "restart"} onClick={() => void checkUpdates()}>
                  <PackageCheck size={15} /> {updateState === "checking" ? "Checking…" : "Check for updates"}
                </button>
              )
            ) : (
              <p>Updates are supplied by your package manager.</p>
            )}
            {updateState === "current" && <p role="status">Nebula is up to date.</p>}
            {updateState === "restart" && <p role="status">Update installed. Restart Nebula to finish.</p>}
            {updateState === "error" && <p className="form-error" role="alert">{updateMessage}</p>}
          </div>
        </section>
      </div>
      </div>
      </div>
      {adding && (selected || editingProvider) && (
        <div className="dialog-backdrop">
          <form className="provider-dialog" role="dialog" aria-modal="true" aria-labelledby="provider-dialog-title" onSubmit={(event) => void submitProvider(event)}>
            <header><div><small>Provider profile</small><h2 id="provider-dialog-title">{editingProvider ? `Edit ${editingProvider.name}` : "Add model provider"}</h2></div><button className="icon-button subtle" type="button" aria-label="Close provider dialog" onClick={() => setAdding(false)}><X size={17} /></button></header>
            <label>Provider type<select value={dialogProviderType} disabled={Boolean(editingProvider)} onChange={(event) => chooseProvider(event.target.value)}>{!providerCatalog.some((entry) => entry.flavor === dialogProviderType) && <option value={dialogProviderType}>{dialogProviderType}</option>}{providerCatalog.map((entry) => <option value={entry.flavor} key={entry.flavor}>{entry.displayName}</option>)}</select></label>
            {editingProvider && <p className="provider-dialog-note">Provider type and locality are fixed after creation. Other profile settings use revision-safe updates.</p>}
            <label>Profile name<input required value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label>Endpoint<input required={!selected?.defaultBaseUrl} value={endpoint} placeholder={selected?.defaultBaseUrl ?? "https://provider.example/v1"} onChange={(event) => setEndpoint(event.target.value)} /></label>
            <label>Default model<input required={requiresDefaultModel} aria-describedby="provider-model-help" value={model} placeholder={requiresDefaultModel ? "Required provider model ID" : "Runtime model ID (optional)"} onChange={(event) => setModel(event.target.value)} /></label>
            <p className="provider-dialog-note" id="provider-model-help">{requiresDefaultModel ? `${dialogProviderType === "bedrock" ? "AWS Bedrock" : "Anthropic"} needs an explicit model ID so a fresh profile can be used immediately for chat and missions.` : !model.trim() && dialogAllowlist.length ? `No explicit default is set. Core will use ${dialogAllowlist[0]}, the first allowed model, as its fallback.` : "When blank with no allowed models, Nebula uses a model discovered by the provider health check."}</p>
            <label>Allowed model IDs<textarea rows={3} value={modelAllowlistText} placeholder="One provider model ID per line" onChange={(event) => setModelAllowlistText(event.target.value)} /></label>
            <p className="provider-dialog-note">The explicit default is added to this allowlist automatically. Remove an old ID here when changing it if that model should no longer be selectable.</p>
            <div className="resource-form-grid"><label>Context window (tokens)<input type="number" min="1" inputMode="numeric" value={contextWindow} placeholder="8192 safe fallback" onChange={(event) => setContextWindow(event.target.value)} /></label><label>Maximum output tokens<input type="number" min="1" inputMode="numeric" value={maxOutputTokens} placeholder="2048 default" onChange={(event) => setMaxOutputTokens(event.target.value)} /></label></div>
            <p className="provider-dialog-note">Nebula compacts at 75% of the available input capacity. When the context window is blank, Core conservatively assumes 8,192 tokens.</p>
            {dialogProviderType === "vertex" && <div className="resource-form-grid"><label>Google Cloud project<input required value={vertexProject} placeholder="my-security-project" onChange={(event) => setVertexProject(event.target.value)} /></label><label>Vertex location<input required value={vertexLocation} placeholder="us-central1" onChange={(event) => setVertexLocation(event.target.value)} /></label></div>}
            {dialogProviderType === "bedrock" && <label>AWS region<input value={awsRegion} placeholder="Uses the ambient AWS region when blank" onChange={(event) => setAwsRegion(event.target.value)} /></label>}
            <label>Credential environment variable<input value={credentialEnv} pattern="[A-Za-z_][A-Za-z0-9_]*" placeholder={dialogLocal ? "Optional for authenticated local gateways" : "For example, OPENAI_API_KEY"} autoCapitalize="none" spellCheck={false} onChange={(event) => setCredentialEnv(event.target.value)} /></label>
            {!dialogLocal && <label className="provider-consent"><input type="checkbox" checked={permitsSensitiveData} onChange={(event) => setPermitsSensitiveData(event.target.checked)} /><span><strong>Allow engagement and document data</strong><small>Permit this profile to receive redacted knowledge excerpts only after a separate confirmation for each chat request.</small></span></label>}
            <p className="provider-dialog-note">{dialogLocal ? "Local-only profile. Nebula will not route it to a cloud fallback." : credentialEnv ? `Core will resolve env:${credentialEnv}; the secret value is never saved in this profile.` : "This profile will use the provider's ambient credential chain, if supported."}</p>
            {selected?.notes && <p className="provider-dialog-note">{selected.notes}</p>}
            <p className="provider-dialog-note">Saving runs one small, harmless required-tool inference probe for the exact default model. Health refresh remains a no-cost liveness check.</p>
            {formError && <p className="form-error" role="alert">{formError}</p>}
            <footer><button className="button secondary" type="button" onClick={() => setAdding(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving || !name.trim() || (requiresDefaultModel && !model.trim()) || (dialogProviderType === "vertex" && (!vertexProject.trim() || !vertexLocation.trim()))}>{saving ? "Saving…" : editingProvider ? "Save provider" : "Add provider"}</button></footer>
          </form>
        </div>
      )}
      {operatorDialog && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="operator-dialog-title" onSubmit={(event) => void submitOperator(event)}><header><div><small>Local attribution</small><h2 id="operator-dialog-title">{editingOperator ? "Edit operator" : "Add operator"}</h2></div><button className="icon-button subtle" type="button" aria-label="Close operator dialog" onClick={() => setOperatorDialog(false)}><X size={17} /></button></header><label>Display name<input required autoFocus value={operatorName} onChange={(event) => setOperatorName(event.target.value)} /></label><label>Email<input type="email" value={operatorEmail} onChange={(event) => setOperatorEmail(event.target.value)} /></label><label>Role<input value={operatorRole} placeholder="Engagement lead, analyst…" onChange={(event) => setOperatorRole(event.target.value)} /></label><p className="provider-dialog-note">This identity is stored locally for attribution only. It is not an authentication account and grants no permissions.</p>{operatorError && <p className="form-error" role="alert">{operatorError}</p>}<footer><button className="button secondary" type="button" onClick={() => setOperatorDialog(false)}>Cancel</button><button className="button primary" type="submit" disabled={Boolean(operatorBusy)}>{operatorBusy ? "Saving…" : "Save operator"}</button></footer></form></div>}
    </div>
  );
}
