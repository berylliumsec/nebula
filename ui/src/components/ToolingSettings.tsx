import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, RefreshCw, Server, ShieldCheck, TerminalSquare, Trash2, UploadCloud } from "lucide-react";
import { ApiError } from "../api/client";
import type { AutomationRuntimeInfo, RunnerProfile, RunnerIsolation, RunnerRuntime, VpnProfile } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { announceSettingsSaved } from "./SettingsSaveFeedback";

function unavailable(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 501);
}

type RunnerSetupKind = "podman_machine" | "docker_desktop" | "podman" | "docker";

const runtimeDefaults: Record<RunnerSetupKind, { name: string; runtime: RunnerRuntime; isolation: Exclude<RunnerIsolation, "unverified">; executable: string; context: string }> = {
  podman_machine: { name: "Podman Machine", runtime: "podman", isolation: "podman_machine", executable: "/opt/homebrew/bin/podman", context: "" },
  docker_desktop: { name: "Docker Desktop", runtime: "docker", isolation: "docker_desktop_vm", executable: "/usr/local/bin/docker", context: "desktop-linux" },
  podman: { name: "Rootless Podman", runtime: "podman", isolation: "rootless", executable: "/usr/bin/podman", context: "" },
  docker: { name: "Rootless Docker", runtime: "docker", isolation: "rootless", executable: "/usr/bin/docker", context: "default" },
};

function profileSetup(profile: RunnerProfile): RunnerSetupKind {
  if (profile.isolationMode === "podman_machine") return "podman_machine";
  if (profile.isolationMode === "docker_desktop_vm") return "docker_desktop";
  return profile.runtimeType;
}

export function AutomationRuntimeSettings() {
  const { api, coreState, previewMode } = useWorkspace();
  const [runtime, setRuntime] = useState<AutomationRuntimeInfo>();
  const [loading, setLoading] = useState(true);
  const [preparing, setPreparing] = useState(false);
  const [error, setError] = useState<string>();
  const [vpnProfiles, setVpnProfiles] = useState<VpnProfile[]>([]);
  const [vpnFile, setVpnFile] = useState<File>();
  const [vpnName, setVpnName] = useState("");
  const [vpnUsername, setVpnUsername] = useState("");
  const [vpnPassword, setVpnPassword] = useState("");
  const [vpnPersistence, setVpnPersistence] = useState<"vault" | "session">("vault");
  const [savingVpn, setSavingVpn] = useState(false);

  const load = useCallback(async () => {
    if (!api || coreState !== "online") {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(undefined);
    try {
      const [nextRuntime, nextVpnProfiles] = await Promise.all([
        api.getAutomationRuntime(),
        api.listVpnProfiles().catch((vpnError) => {
          if (unavailable(vpnError)) return [];
          void logCaughtDiagnostic("interface.automation_runtime.vpn_profiles_unavailable", "VPN profiles are unavailable in this Core build.", vpnError, "automation_runtime");
          return Promise.reject(vpnError);
        }),
      ]);
      setRuntime(nextRuntime);
      setVpnProfiles(nextVpnProfiles);
    } catch (loadError) {
      void logCaughtDiagnostic("interface.automation_runtime.caught_failure_01", "A handled interface operation failed.", loadError, "automation_runtime");
      setError(loadError instanceof Error ? loadError.message : "Could not load the automation runtime.");
    } finally {
      setLoading(false);
    }
  }, [api, coreState]);

  useEffect(() => { void load(); }, [load]);

  const prepare = async () => {
    if (!api) return;
    setPreparing(true);
    setError(undefined);
    try {
      setRuntime(await api.prepareAutomationRuntime());
    } catch (prepareError) {
      void logCaughtDiagnostic("interface.automation_runtime.caught_failure_02", "A handled interface operation failed.", prepareError, "automation_runtime");
      setError(prepareError instanceof Error ? prepareError.message : "Could not prepare the automation runtime.");
    } finally {
      setPreparing(false);
    }
  };

  const uploadVpn = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !vpnFile) return;
    setSavingVpn(true); setError(undefined);
    try {
      const saved = await api.createVpnProfile({
        name: vpnName.trim() || vpnFile.name.replace(/\.ovpn$/i, ""), filename: vpnFile.name,
        config: await vpnFile.text(), username: vpnUsername || undefined, password: vpnPassword || undefined,
        persistence: vpnPersistence,
      });
      setVpnProfiles((items) => [saved, ...items]); setVpnFile(undefined); setVpnName(""); setVpnUsername(""); setVpnPassword("");
      announceSettingsSaved("VPN profile saved. Select it from a project's command runtime policy.");
    } catch (uploadError) {
      void logCaughtDiagnostic("interface.automation_runtime.vpn_upload_failed", "VPN profile could not be saved.", uploadError, "automation_runtime");
      setError(uploadError instanceof Error ? uploadError.message : "Could not save the VPN profile.");
    } finally { setSavingVpn(false); }
  };

  const removeVpn = async (profile: VpnProfile) => {
    if (!api) return;
    setError(undefined);
    try { await api.deleteVpnProfile(profile.id, profile.revision); setVpnProfiles((items) => items.filter((item) => item.id !== profile.id)); }
    catch (deleteError) {
      void logCaughtDiagnostic("interface.automation_runtime.vpn_delete_failed", "VPN profile could not be removed.", deleteError, "automation_runtime");
      setError(deleteError instanceof Error ? deleteError.message : "Could not remove the VPN profile.");
    }
  };

  return <section className="settings-section" id="automation-runtime-settings">
    <div className="section-heading"><div><h2>Automation runtime</h2><p>One digest-pinned Bash container per agent session. Commands use ordinary binaries on PATH.</p></div><button className="button secondary" type="button" disabled={loading || preparing} onClick={() => void load()}><RefreshCw size={14} /> Refresh</button></div>
    {error && <DiagnosticErrorNotice error={error} fallback="The automation runtime could not be inspected." compact />}
    {!runtime ? <div className="feature-unavailable" role="status"><AlertTriangle size={22} /><div><strong>{loading ? "Checking runtime…" : "Runtime unavailable"}</strong><p>Configure a pinned automation image and a verified local runner.</p></div></div> :
      <div className="runner-layout"><section className="panel runner-status"><header className="panel-header compact"><div><h3>{runtime.ready ? "Ready" : "Preparation required"}</h3><p>{runtime.detail}</p></div>{runtime.ready ? <CheckCircle2 size={18} /> : <AlertTriangle size={18} />}</header><article><TerminalSquare size={17} /><div><strong>{runtime.digest ?? "No prepared digest"}</strong><small>{runtime.runnerProfileId ? `Runner ${runtime.runnerProfileId}` : "No healthy runner selected"}</small><p>{runtime.image ?? "Prepare Nebula's existing Kali headless image."}</p></div></article><footer><button className="button primary" type="button" disabled={previewMode || preparing || !runtime.configured} onClick={() => void prepare()}>{preparing ? "Preparing Kali…" : runtime.ready ? "Verify Kali runtime" : "Prepare Kali runtime"}</button></footer></section><section className="panel inventory-panel"><details className="inventory-disclosure"><summary><div><h3>Binary inventory</h3><p>Generated from the exact prepared Kali image.</p></div><span className="inventory-count">{runtime.inventory.length.toLocaleString()}</span><ChevronDown className="inventory-chevron" size={18} aria-hidden="true" /></summary><div className="runtime-resource-list">{runtime.inventory.length ? runtime.inventory.map((binary) => <article className="runtime-resource-card" key={`${binary.path}-${binary.name}`}><header><div><strong>{binary.name}</strong><code>{binary.path}</code></div><small>{binary.version}</small></header></article>) : <div className="empty-state compact"><TerminalSquare size={21} /><strong>No verified inventory</strong><p>Prepare the Kali runtime to validate its installed binaries.</p></div>}</div></details></section></div>}
    <div className="section-heading vpn-heading"><div><h2>VPN egress</h2><p>Reusable OpenVPN profiles for project command containers. Traffic remains scope-limited inside the tunnel.</p></div><ShieldCheck size={20} /></div>
    <div className="runner-layout">
      <form className="panel runner-form vpn-profile-form" onSubmit={(event) => void uploadVpn(event)}>
        <label className="vpn-file-drop"><UploadCloud size={22} /><span><strong>{vpnFile?.name ?? "Choose an OpenVPN profile"}</strong><small>.ovpn · TUN client · inline certificates · up to 15 KB</small></span><input type="file" accept=".ovpn,application/x-openvpn-profile" required onChange={(event) => { const file = event.target.files?.[0]; setVpnFile(file); if (file && !vpnName) setVpnName(file.name.replace(/\.ovpn$/i, "")); }} /></label>
        <label>Profile name<input value={vpnName} placeholder="Company VPN" onChange={(event) => setVpnName(event.target.value)} /></label>
        <details className="inventory-disclosure vpn-options"><summary><span><strong>Credentials and storage</strong><small>Only needed when the profile requests username/password authentication.</small></span><ChevronDown size={17} /></summary><div className="policy-form-body"><label>Username<input autoComplete="username" value={vpnUsername} onChange={(event) => setVpnUsername(event.target.value)} /></label><label>Password<input type="password" autoComplete="new-password" value={vpnPassword} onChange={(event) => setVpnPassword(event.target.value)} /></label><label>Storage<select value={vpnPersistence} onChange={(event) => setVpnPersistence(event.target.value as "vault" | "session")}><option value="vault">Operating-system vault</option><option value="session">This Core session only</option></select></label></div></details>
        <footer><span>Scripts, plugins, TAP, split routes, and external file paths are rejected.</span><button className="button primary" type="submit" disabled={!vpnFile || savingVpn || previewMode}>{savingVpn ? "Checking…" : "Save profile"}</button></footer>
      </form>
      <section className="panel runner-status"><header className="panel-header compact"><div><h3>Saved profiles</h3><p>Raw configuration and credentials are never displayed again.</p></div><span className="inventory-count">{vpnProfiles.length}</span></header>{vpnProfiles.length ? vpnProfiles.map((profile) => <article key={profile.id}><span className={`status-dot ${profile.available ? "healthy" : "unavailable"}`} /><div><strong>{profile.name}</strong><small>{profile.protocol.toUpperCase()} · {profile.remoteHost}:{profile.remotePort}</small><p>{profile.available ? "Stored securely; the tunnel is verified when a new session starts." : "Secret is unavailable; upload the profile again."}</p></div><button className="icon-button" type="button" aria-label={`Remove ${profile.name}`} onClick={() => void removeVpn(profile)}><Trash2 size={15} /></button></article>) : <div className="empty-state compact"><ShieldCheck size={21} /><strong>No VPN profiles</strong><p>Upload one here, then select it in a project’s command runtime policy.</p></div>}</section>
    </div>
  </section>;
}

export function RunnerSettings() {
  const { api, coreState, health, previewMode, runtime } = useWorkspace();
  const [profiles, setProfiles] = useState<RunnerProfile[]>([]);
  const [selectedId, setSelectedId] = useState("local");
  const [setupKind, setSetupKind] = useState<RunnerSetupKind>("podman_machine");
  const [name, setName] = useState(runtimeDefaults.podman_machine.name);
  const [executable, setExecutable] = useState(runtimeDefaults.podman_machine.executable);
  const [context, setContext] = useState("");
  const [socket, setSocket] = useState("");
  const [platform, setPlatform] = useState("linux/arm64");
  const [seccompProfile, setSeccompProfile] = useState("");
  const [available, setAvailable] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();

  const load = useCallback(async () => {
    if (!api || coreState !== "online") return;
    try {
      const next = await api.listRunnerProfiles();
      setAvailable(true);
      setProfiles(next);
      const selected = next.find((profile) => profile.id === selectedId) ?? next[0];
      if (selected) {
        setSelectedId(selected.id); setSetupKind(profileSetup(selected)); setName(selected.name); setExecutable(selected.executable); setContext(selected.context ?? ""); setSocket(selected.socket ?? ""); setPlatform(selected.platform); setSeccompProfile(selected.seccompProfile ?? "");
      }
    } catch (loadError) {
      void logCaughtDiagnostic("interface.tooling_settings.caught_failure_02", "A handled interface operation failed.", loadError, "tooling_settings");
      if (unavailable(loadError)) setAvailable(false);
      else setError(loadError instanceof Error ? loadError.message : "Could not load runner profiles.");
    }
  }, [api, coreState, selectedId]);

  useEffect(() => { void load(); }, [load]);

  const chooseRuntime = (value: RunnerSetupKind) => {
    const defaults = runtimeDefaults[value];
    setSetupKind(value); setName(defaults.name); setExecutable(defaults.executable); setContext(defaults.context); setSocket("");
  };

  const chooseProfile = (id: string) => {
    setSelectedId(id);
    const profile = profiles.find((item) => item.id === id);
    if (profile) { setSetupKind(profileSetup(profile)); setName(profile.name); setExecutable(profile.executable); setContext(profile.context ?? ""); setSocket(profile.socket ?? ""); setPlatform(profile.platform); setSeccompProfile(profile.seccompProfile ?? ""); }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api) return;
    setSaving(true); setError(undefined);
    try {
      const current = profiles.find((profile) => profile.id === selectedId);
      const defaults = runtimeDefaults[setupKind];
      const saved = await api.updateRunnerProfile(selectedId, { name, runtimeType: defaults.runtime, isolationMode: defaults.isolation, executable, context: context || undefined, socket: socket || undefined, platform, seccompProfile: seccompProfile || undefined, expectedRevision: current?.revision });
      setProfiles((items) => [saved, ...items.filter((profile) => profile.id !== saved.id)]);
      setSelectedId(saved.id);
      announceSettingsSaved("Runner profile verified and updated.");
    } catch (saveError) {
      void logCaughtDiagnostic("interface.tooling_settings.caught_failure_03", "A handled interface operation failed.", saveError, "tooling_settings");
      setError(saveError instanceof Error ? saveError.message : "Could not save the runner profile.");
    } finally { setSaving(false); }
  };

  return <section className="settings-section" id="runtime-settings"><div className="section-heading"><div><h2>Sandbox runners</h2><p>Select a trusted absolute executable and local runtime context. Nebula never discovers a runner through PATH.</p></div>{profiles.length > 1 && <label className="inline-select">Profile<select aria-label="Runner profile" value={selectedId} onChange={(event) => chooseProfile(event.target.value)}>{profiles.map((profile) => <option value={profile.id} key={profile.id}>{profile.name}</option>)}</select></label>}</div>{!available ? <div className="feature-unavailable" role="status"><Server size={22} /><div><strong>Runner profiles are not available in this Core build</strong><p>Core reports {health?.runner ?? "unavailable"}; missions remain analysis-only.</p></div></div> : <div className="runner-layout"><form className="runner-form panel" onSubmit={(event) => void save(event)}><label>Runtime<select value={setupKind} onChange={(event) => chooseRuntime(event.target.value as RunnerSetupKind)}><option value="podman_machine">Podman Machine · macOS</option><option value="docker_desktop">Docker Desktop · macOS</option><option value="podman">Rootless Podman · Linux</option><option value="docker">Rootless Docker · Linux</option></select></label><label>Profile name<input required value={name} onChange={(event) => setName(event.target.value)} /></label><label>Trusted executable<input required value={executable} pattern="/.*" spellCheck={false} onChange={(event) => setExecutable(event.target.value)} /></label><label>Container platform<select value={platform} onChange={(event) => setPlatform(event.target.value)}><option value="linux/arm64">Linux ARM64</option><option value="linux/amd64">Linux AMD64</option></select></label><label>Local context<input value={context} placeholder="Optional local runtime context" onChange={(event) => setContext(event.target.value)} /></label><label>Local Unix socket<input value={socket} pattern="^$|(?:unix://)?/.*" placeholder="Optional absolute Unix socket path" spellCheck={false} onChange={(event) => setSocket(event.target.value)} /></label><label>Seccomp profile<input value={seccompProfile} pattern="^$|/.*" placeholder="Optional absolute local profile path" spellCheck={false} onChange={(event) => setSeccompProfile(event.target.value)} /></label>{error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}<footer><span>No remote TCP endpoints or automatic runtime installation.</span><button className="button primary" type="submit" disabled={saving || previewMode}>{saving ? "Checking…" : "Save and check"}</button></footer></form><aside className="panel runner-status"><header className="panel-header compact"><div><h3>Isolation status</h3><p>{runtime?.mode ?? "desktop"} control plane</p></div><Server size={18} /></header>{profiles.length ? profiles.map((profile) => <article key={profile.id}><span className={`status-dot ${profile.state === "ready" ? "healthy" : "unavailable"}`} /><div><strong>{profile.name}</strong><small>{profile.state} · {profile.isolationMode.replaceAll("_", " ")} · {profile.platform}</small>{profile.detail && <p>{profile.detail}</p>}<p>The automation runtime supplies its digest-pinned Kali egress helper.</p></div></article>) : <div className="empty-state compact"><Server size={21} /><strong>No explicit runner profile</strong><p>Save this profile to ask Core to verify the local runtime and isolation boundary.</p></div>}</aside></div>}</section>;
}
