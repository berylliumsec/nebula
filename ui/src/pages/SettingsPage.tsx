import { useState, type FormEvent } from "react";
import { Contrast, KeyRound, Moon, Plus, Server, ShieldCheck, Sun, X } from "lucide-react";
import type { ProviderCatalogEntry } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { ProviderHealthCard } from "../components/ProviderHealthCard";
import { useTheme, type ThemePreference } from "../state/ThemeContext";
import { useWorkspace } from "../state/WorkspaceContext";

const themeOptions: { value: ThemePreference; label: string; icon: typeof Sun }[] = [
  { value: "system", label: "System", icon: Contrast },
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
  { value: "high-contrast", label: "High contrast", icon: Contrast },
];

export function SettingsPage() {
  const { preference, setPreference } = useTheme();
  const {
    previewMode,
    runtime,
    health,
    providers,
    providerCatalog,
    refreshProvider,
    addProvider,
  } = useWorkspace();
  const [adding, setAdding] = useState(false);
  const [selected, setSelected] = useState<ProviderCatalogEntry>();
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [model, setModel] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string>();

  const openProviderDialog = () => {
    const entry = providerCatalog.find((item) => item.flavor === "vllm") ?? providerCatalog[0];
    if (!entry) return;
    setSelected(entry);
    setName(entry.flavor === "vllm" ? "Local vLLM" : entry.displayName);
    setEndpoint(entry.defaultBaseUrl ?? "");
    setModel("");
    setFormError(undefined);
    setAdding(true);
  };

  const chooseProvider = (flavor: string) => {
    const entry = providerCatalog.find((item) => item.flavor === flavor);
    if (!entry) return;
    setSelected(entry);
    setName(entry.displayName);
    setEndpoint(entry.defaultBaseUrl ?? "");
  };

  const submitProvider = async (event: FormEvent) => {
    event.preventDefault();
    if (!selected) return;
    setSaving(true);
    setFormError(undefined);
    try {
      await addProvider({
        name,
        providerType: selected.flavor,
        endpoint: endpoint || undefined,
        local: selected.local,
        defaultModel: model || undefined,
      });
      setAdding(false);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Could not add provider.");
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="page settings-page">
      <PageHeader eyebrow="Workspace configuration" title="Settings" description="Explicit provider, runner, privacy, access, and appearance controls." />
      <nav className="settings-tabs" aria-label="Settings sections"><button className="active" type="button">Providers</button><button type="button">Runners</button><button type="button">Security</button><button type="button">Team access</button><button type="button">Appearance</button></nav>
      <section className="settings-section">
        <div className="section-heading"><div><h2>Model providers</h2><p>Configured provider profiles and their declared capabilities.</p></div><button className="button primary" type="button" disabled={previewMode || providerCatalog.length === 0} onClick={openProviderDialog}><Plus size={16} /> Add provider</button></div>
        {providers.length > 0 ? (
          <div className="provider-grid">{providers.map((provider) => <ProviderHealthCard provider={provider} preview={previewMode} onRefresh={refreshProvider} key={provider.id} />)}</div>
        ) : (
          <div className="empty-state compact"><Server size={23} /><strong>No provider profiles</strong><p>Add a provider profile in Core before assigning a model to a mission.</p></div>
        )}
      </section>
      <div className="settings-bottom-grid">
        <section className="panel runtime-panel">
          <header className="panel-header compact"><div><h2>Local runtime</h2><p>Desktop control-plane boundary</p></div><Server size={19} /></header>
          <dl><div><dt>Shell</dt><dd>{runtime?.mode ?? "Detecting…"}</dd></div><div><dt>Core version</dt><dd>{health?.version ?? "Unavailable"}</dd></div><div><dt>Sandbox runner</dt><dd><span className={`status-dot ${health?.runner === "ready" ? "healthy" : "unavailable"}`} /> {health?.runner ?? "Not connected"}</dd></div><div><dt>Host fallback</dt><dd><span className="status-dot healthy" /> Disabled</dd></div></dl>
          <div className="security-note"><ShieldCheck size={17} /><span><strong>Secure by construction</strong><small>The desktop shell only starts a fixed sibling binary on loopback and transfers its one-time token over stdin.</small></span></div>
        </section>
        <section className="panel appearance-panel">
          <header className="panel-header compact"><div><h2>Appearance</h2><p>Saved on this device</p></div><Contrast size={19} /></header>
          <div className="theme-options">
            {themeOptions.map(({ value, label, icon: Icon }) => (
              <button key={value} type="button" aria-pressed={preference === value} onClick={() => setPreference(value)}><span><Icon size={18} /></span><strong>{label}</strong>{preference === value && <small>Active</small>}</button>
            ))}
          </div>
          <p className="appearance-help">All themes preserve visible keyboard focus. High contrast strengthens boundaries and meets system forced-color expectations.</p>
        </section>
        <section className="panel secrets-panel">
          <header className="panel-header compact"><div><h2>Credential references</h2><p>Secrets never enter agent context</p></div><KeyRound size={19} /></header>
          <div className="empty-state compact"><KeyRound size={23} /><strong>Managed outside prompts</strong><p>Core resolves credential references only inside an approved tool sandbox.</p><button className="button secondary" type="button">Manage references</button></div>
        </section>
      </div>
      {adding && selected && (
        <div className="dialog-backdrop">
          <form className="provider-dialog" role="dialog" aria-modal="true" aria-labelledby="provider-dialog-title" onSubmit={(event) => void submitProvider(event)}>
            <header><div><small>Provider profile</small><h2 id="provider-dialog-title">Add model provider</h2></div><button className="icon-button subtle" type="button" aria-label="Close provider dialog" onClick={() => setAdding(false)}><X size={17} /></button></header>
            <label>Provider type<select value={selected.flavor} onChange={(event) => chooseProvider(event.target.value)}>{providerCatalog.map((entry) => <option value={entry.flavor} key={entry.flavor}>{entry.displayName}</option>)}</select></label>
            <label>Profile name<input required value={name} onChange={(event) => setName(event.target.value)} /></label>
            <label>Endpoint<input required={!selected.defaultBaseUrl} value={endpoint} placeholder={selected.defaultBaseUrl ?? "https://provider.example/v1"} onChange={(event) => setEndpoint(event.target.value)} /></label>
            <label>Default model<input value={model} placeholder="Runtime model ID (optional)" onChange={(event) => setModel(event.target.value)} /></label>
            <p className="provider-dialog-note">{selected.local ? "Local-only profile. Nebula will not route it to a cloud fallback." : "Credentials are stored as environment references after profile creation."}</p>
            {formError && <p className="form-error" role="alert">{formError}</p>}
            <footer><button className="button secondary" type="button" onClick={() => setAdding(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving}>{saving ? "Saving…" : "Add provider"}</button></footer>
          </form>
        </div>
      )}
    </div>
  );
}
