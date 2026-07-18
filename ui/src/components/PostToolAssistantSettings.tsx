import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Check, Cpu, LoaderCircle, Sparkles } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { HarnessProfile, PostToolAssistantConfig, ProviderHealth } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { announceSettingsSaved } from "./SettingsSaveFeedback";

interface Props {
  api?: ApiClient;
  engagementId?: string;
  providers: ProviderHealth[];
  previewMode?: boolean;
}

const EMPTY: PostToolAssistantConfig = {
  suggestNextSteps: false,
  takeNotes: false,
  backendKind: "provider",
  cloudConfirmed: false,
};

function backendValue(config: PostToolAssistantConfig): string {
  const id = config.backendKind === "harness" ? config.harnessProfileId : config.providerId;
  return id ? `${config.backendKind}:${id}` : "";
}

function providerModels(provider: ProviderHealth): string[] {
  return [...new Set([
    ...(provider.effectiveDefaultModel ? [provider.effectiveDefaultModel] : []),
    ...(provider.defaultModel ? [provider.defaultModel] : []),
    ...(provider.availableModels ?? []),
    ...provider.models,
    ...provider.modelAllowlist,
  ])];
}

function harnessModels(harness: HarnessProfile): string[] {
  return [...new Set([...(harness.defaultModel ? [harness.defaultModel] : []), ...harness.models])];
}

export function PostToolAssistantSettings({ api, engagementId, providers, previewMode = false }: Props) {
  const [config, setConfig] = useState<PostToolAssistantConfig>(EMPTY);
  const [harnesses, setHarnesses] = useState<HarnessProfile[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    let active = true;
    if (!api || !engagementId) return () => { active = false; };
    setLoading(true);
    void Promise.all([api.getPostToolAssistant(engagementId), api.listHarnesses()])
      .then(([nextConfig, nextHarnesses]) => {
        if (!active) return;
        setConfig(nextConfig);
        setHarnesses(nextHarnesses);
        setError(undefined);
      })
      .catch((caught) => {
        void logCaughtDiagnostic("interface.post_tool_assistant_settings.load_failed", "Post-tool assistant settings could not be loaded.", caught, "post_tool_assistant_settings");
        if (active) setError(caught instanceof Error ? caught.message : "Tool follow-up settings are unavailable.");
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [api, engagementId]);

  const enabledProviders = providers.filter((item) => item.enabled);
  const enabledHarnesses = harnesses.filter((item) => item.enabled);
  const selectedProvider = config.backendKind === "provider"
    ? enabledProviders.find((item) => item.id === config.providerId)
    : undefined;
  const selectedHarness = config.backendKind === "harness"
    ? enabledHarnesses.find((item) => item.id === config.harnessProfileId)
    : undefined;
  const selectedBackend = selectedHarness ?? selectedProvider;
  const remote = selectedHarness ? !selectedHarness.localOnly : selectedProvider ? !selectedProvider.local : false;
  const permitsProjectData = selectedHarness?.permitsSensitiveData ?? selectedProvider?.permitsSensitiveData ?? true;
  const models = useMemo(() => {
    const discovered = selectedHarness
      ? harnessModels(selectedHarness)
      : selectedProvider
        ? providerModels(selectedProvider)
        : [];
    return [...new Set([...(config.model ? [config.model] : []), ...discovered])];
  }, [config.model, selectedHarness, selectedProvider]);
  const ready = Boolean(selectedBackend && config.model?.trim() && (!remote || (permitsProjectData && config.cloudConfirmed)));

  const chooseBackend = (value: string) => {
    if (!value) {
      setConfig((current) => ({ ...current, providerId: undefined, harnessProfileId: undefined, model: undefined, cloudConfirmed: false }));
      return;
    }
    const [kind, id] = value.split(":", 2) as ["provider" | "harness", string];
    const backend = kind === "harness"
      ? enabledHarnesses.find((item) => item.id === id)
      : enabledProviders.find((item) => item.id === id);
    const defaultModel = backend
      ? "effectiveDefaultModel" in backend
        ? backend.effectiveDefaultModel ?? backend.defaultModel ?? backend.models[0]
        : backend.defaultModel ?? backend.models[0]
      : undefined;
    setConfig((current) => ({
      ...current,
      backendKind: kind,
      providerId: kind === "provider" ? id : undefined,
      harnessProfileId: kind === "harness" ? id : undefined,
      model: defaultModel,
      cloudConfirmed: false,
    }));
    setError(undefined);
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !engagementId || !selectedBackend || !config.model?.trim()) return;
    if (remote && !permitsProjectData) {
      setError(`${selectedBackend.name} does not allow project data. Enable that permission on its profile before using it for tool follow-up.`);
      return;
    }
    if (remote && !config.cloudConfirmed) {
      setError("Confirm cloud data use for this project before saving a remote analysis runtime.");
      return;
    }
    setSaving(true);
    setError(undefined);
    try {
      const saved = await api.setPostToolAssistant(engagementId, { ...config, model: config.model.trim(), cloudConfirmed: remote ? config.cloudConfirmed : false });
      setConfig(saved);
      announceSettingsSaved("Tool follow-up runtime saved for this project.");
    } catch (caught) {
      void logCaughtDiagnostic("interface.post_tool_assistant_settings.save_failed", "Post-tool assistant settings could not be saved.", caught, "post_tool_assistant_settings");
      setError(caught instanceof Error ? caught.message : "Could not save tool follow-up settings.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="settings-section" id="post-tool-assistant-settings">
      <div className="section-heading"><div><h2>Tool follow-up</h2><p>Notes and next-step suggestions</p></div>{ready && <span className="post-tool-settings-ready"><Check size={14} /> Ready</span>}</div>
      <form className="panel post-tool-settings-panel" onSubmit={(event) => void save(event)}>
        <div className="post-tool-settings-intro">
          <span><Sparkles size={18} /></span>
          <div><h3>Analysis runtime</h3><p>Choose the model that reads completed tool output. Turn notes and suggestions on only when you need them from the Workbench.</p></div>
        </div>
        <div className="post-tool-settings-fields">
          <label>Runtime<select aria-label="Tool follow-up runtime" value={backendValue(config)} disabled={loading || saving || previewMode} onChange={(event) => chooseBackend(event.target.value)}><option value="">Choose a model or harness</option>{enabledProviders.length > 0 && <optgroup label="Model providers">{enabledProviders.map((item) => <option value={`provider:${item.id}`} key={item.id}>{item.name}{item.local ? " · local" : " · cloud"}</option>)}</optgroup>}{enabledHarnesses.length > 0 && <optgroup label="Agent harnesses">{enabledHarnesses.map((item) => <option value={`harness:${item.id}`} key={item.id}>{item.name}{item.localOnly ? " · local" : " · cloud"}</option>)}</optgroup>}</select></label>
          <label>Model<input aria-label="Tool follow-up model" list="post-tool-analysis-models" value={config.model ?? ""} disabled={!selectedBackend || loading || saving || previewMode} placeholder={selectedHarness ? "Model name or harness alias" : "Select or enter a model"} onChange={(event) => setConfig((current) => ({ ...current, model: event.target.value || undefined }))} /><datalist id="post-tool-analysis-models">{models.map((item) => <option value={item} key={item} />)}</datalist></label>
        </div>
        {remote && <label className="post-tool-cloud-consent"><input type="checkbox" checked={config.cloudConfirmed} disabled={!permitsProjectData || saving || previewMode} onChange={(event) => setConfig((current) => ({ ...current, cloudConfirmed: event.target.checked }))} /><span><strong>Allow redacted tool output for this project</strong><small>{permitsProjectData ? `Nebula may send bounded stdout and stderr to ${selectedBackend?.name}.` : `${selectedBackend?.name} is not configured to receive project data.`}</small></span></label>}
        {!selectedBackend && !loading && <p className="post-tool-settings-hint"><Cpu size={14} /> Add or enable a model provider or agent harness in Settings, then select it here.</p>}
        <footer><span>{config.suggestNextSteps || config.takeNotes ? `${config.suggestNextSteps ? "Suggestions" : ""}${config.suggestNextSteps && config.takeNotes ? " and " : ""}${config.takeNotes ? "notes" : ""} currently enabled` : "Workbench controls are currently off"}</span><button className="button primary" type="submit" disabled={!selectedBackend || !config.model?.trim() || saving || loading || previewMode || (remote && (!permitsProjectData || !config.cloudConfirmed))}>{saving ? <><LoaderCircle className="spin" size={14} /> Saving…</> : "Save runtime"}</button></footer>
        {error && <DiagnosticErrorNotice error={error} fallback="Tool follow-up settings could not be saved." compact />}
      </form>
    </section>
  );
}
