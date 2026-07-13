import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { AlertTriangle, CheckCircle2, Package, RefreshCw, Server, ShieldCheck, Trash2, Upload } from "lucide-react";
import { ApiError } from "../api/client";
import type { StreamState } from "../api/events";
import { ToolPackEventStream, type ToolPackProgressEvent } from "../api/toolPackEvents";
import type {
  RunnerProfile,
  RunnerIsolation,
  RunnerRuntime,
  ToolPackCatalogEntry,
  ToolPackInstallation,
  ToolSummary,
} from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { useConfirmation } from "./DialogSystem";

function unavailable(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 501);
}

function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("Could not read the environment bundle."));
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.readAsDataURL(file);
  });
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

function packLabel(pack: Pick<ToolPackInstallation, "publisher" | "name" | "version">): string {
  return `${pack.publisher}/${pack.name} · ${pack.version}`;
}

function progressStreamLabel(state: StreamState): string {
  if (state === "open") return "Live";
  if (state === "connecting") return "Connecting";
  if (state === "reconnecting") return "Reconnecting with replay";
  if (state === "unsupported") return "Live progress unavailable";
  return "Disconnected";
}

function progressWidth(phase: ToolPackProgressEvent["phase"]): string {
  if (phase === "pending") return "12%";
  if (phase === "pulling") return "45%";
  if (phase === "verifying") return "76%";
  return "100%";
}

export function ToolPackSettings() {
  const confirm = useConfirmation();
  const { api, coreState, previewMode } = useWorkspace();
  const [catalog, setCatalog] = useState<ToolPackCatalogEntry[]>([]);
  const [packs, setPacks] = useState<ToolPackInstallation[]>([]);
  const [tools, setTools] = useState<ToolSummary[]>([]);
  const [runners, setRunners] = useState<RunnerProfile[]>([]);
  const [featureAvailable, setFeatureAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();
  const [localBundle, setLocalBundle] = useState<File>();
  const [developerConfirmed, setDeveloperConfirmed] = useState(false);
  const [progressEvents, setProgressEvents] = useState<ToolPackProgressEvent[]>([]);
  const [progressStreamState, setProgressStreamState] = useState<StreamState>("closed");
  const progressCursor = useRef(0);

  const load = useCallback(async () => {
    if (!api || coreState !== "online") {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(undefined);
    const results = await Promise.allSettled([
      api.listToolCatalog(),
      api.listToolPacks(),
      api.listTools(),
      api.listRunnerProfiles(),
    ]);
    const unsupported = results[0].status === "rejected" && unavailable(results[0].reason);
    setFeatureAvailable(!unsupported);
    if (results[0].status === "fulfilled") setCatalog(results[0].value as ToolPackCatalogEntry[]);
    if (results[1].status === "fulfilled") setPacks(results[1].value as ToolPackInstallation[]);
    if (results[2].status === "fulfilled") setTools(results[2].value as ToolSummary[]);
    if (results[3].status === "fulfilled") setRunners(results[3].value as RunnerProfile[]);
    const failure = results.find((result) => result.status === "rejected" && !unavailable(result.reason));
    if (failure?.status === "rejected") setError(failure.reason instanceof Error ? failure.reason.message : "Could not load execution-environment status.");
    setLoading(false);
  }, [api, coreState]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!api || coreState !== "online" || !featureAvailable) {
      setProgressStreamState(featureAvailable ? "closed" : "unsupported");
      return;
    }
    const stream = new ToolPackEventStream({
      apiBaseUrl: api.baseUrl,
      token: api.getToken(),
      afterSequence: progressCursor.current,
      onStateChange: setProgressStreamState,
      onReplayGap: () => void load(),
      onEvent: (event) => {
        progressCursor.current = event.sequence;
        setProgressEvents((current) => [event, ...current.filter((item) => item.operationId !== event.operationId)]
          .sort((left, right) => right.sequence - left.sequence)
          .slice(0, 8));
        if (event.phase === "ready" || event.phase === "failed") void load();
      },
    });
    stream.connect();
    return () => stream.disconnect();
  }, [api, coreState, featureAvailable, load]);

  const readyRunner = runners.find((runner) => runner.state === "ready");
  const installedDigests = useMemo(() => new Set(packs.map((pack) => pack.manifestDigest)), [packs]);
  const collections = useMemo(() => {
    const grouped = new Map<string, { id: string; name: string; entries: ToolPackCatalogEntry[] }>();
    for (const entry of catalog) {
      if (!entry.collectionId || !entry.collectionName) continue;
      const collection = grouped.get(entry.collectionId) ?? { id: entry.collectionId, name: entry.collectionName, entries: [] };
      collection.entries.push(entry);
      grouped.set(entry.collectionId, collection);
    }
    return [...grouped.values()].map((collection) => ({
      ...collection,
      entries: collection.entries.sort((left, right) => left.collectionOrder - right.collectionOrder),
    }));
  }, [catalog]);

  const action = async (id: string, operation: () => Promise<unknown>) => {
    setBusy(id);
    setError(undefined);
    try {
      await operation();
      await load();
      return true;
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "The execution-environment operation failed.");
      return false;
    } finally {
      setBusy(undefined);
    }
  };

  const install = (entry: ToolPackCatalogEntry) => {
    if (!api || !readyRunner) return;
    void action(entry.id, () => api.installToolPack(entry.id, readyRunner.id, entry.version));
  };

  const installCollection = (collectionId: string) => {
    if (!api || !readyRunner) return;
    void action(`collection:${collectionId}`, () => api.installToolCollection(collectionId, readyRunner.id));
  };

  const installLocal = async () => {
    if (!api || !readyRunner || !localBundle || !developerConfirmed) return;
    const installed = await action("local", async () => {
      const bundle = await fileBase64(localBundle);
      return api.installLocalToolPack(bundle, readyRunner.id, true);
    });
    if (installed) {
      setLocalBundle(undefined);
      setDeveloperConfirmed(false);
    }
  };

  const removePack = async (pack: ToolPackInstallation) => {
    if (!api || !await confirm({
      title: `Remove ${pack.name}?`,
      message: "The environment will be disabled and removed. Historical manifest locks and evidence will be retained.",
      confirmLabel: "Remove environment",
      tone: "danger",
    })) return;
    await action(pack.id, () => api.removeToolPack(pack.id));
  };

  if (!featureAvailable) {
    return <section className="settings-section" id="tool-pack-settings"><div className="feature-unavailable" role="status"><Package size={22} /><div><strong>Execution environments are not available in this Core build</strong><p>Missions remain analysis-only. Upgrade Core when the signed environment API is available.</p></div></div></section>;
  }

  return (
    <section className="settings-section" id="tool-pack-settings">
      <div className="section-heading"><div><h2>Execution environment</h2><p>One signed, digest-pinned Toolbox image containing the commands agents discover and run.</p></div><button className="button secondary" type="button" disabled={loading || previewMode} onClick={() => void load()}><RefreshCw size={14} /> Refresh</button></div>
      {error && <div className="knowledge-status error" role="alert">{error}</div>}
      {!readyRunner && <div className="knowledge-status warning" role="status"><AlertTriangle size={15} /> Configure a runner before installing an environment. Commands remain unavailable to missions.</div>}
      <section className="tool-progress" aria-label="Tool-pack installation progress" aria-live="polite">
        <header><div><strong>Installation progress</strong><small>Authenticated replay stream</small></div><span className={`progress-stream-state ${progressStreamState}`}><span className="status-dot" /> {progressStreamLabel(progressStreamState)}</span></header>
        {progressEvents.length ? <div className="tool-progress-list">{progressEvents.map((event) => <article className={event.phase} key={event.operationId}><div><strong>{event.packIdentity ?? event.operation.replaceAll("_", " ")}</strong><small>{event.operation.replaceAll("_", " ")}{event.manifestDigest ? ` · ${event.manifestDigest.slice(0, 12)}…` : ""}</small></div><span>{event.resultStatus ?? event.phase}</span><div className="progress-track small" aria-hidden="true"><span style={{ width: progressWidth(event.phase) }} /></div><time dateTime={event.occurredAt}>{new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(new Date(event.occurredAt))}</time></article>)}</div> : <p>{progressStreamState === "unsupported" ? "Live progress is unavailable in this Core. Environment status can still be refreshed manually." : "No environment operations have been observed in this session."}</p>}
      </section>
      {collections.length > 0 && <section className="tool-collections" aria-label="Execution environments"><div className="section-heading"><div><h3>Official Toolbox</h3><p>Install the reviewed environment once; each engagement still receives an exact digest and capability assignment.</p></div></div><div className="tool-pack-list">{collections.map((collection) => {
        const complete = collection.entries.every((entry) => installedDigests.has(entry.manifestDigest));
        const busyId = `collection:${collection.id}`;
        const toolCount = collection.entries.reduce((count, entry) => count + entry.toolNames.length, 0);
        const interfaceCount = collection.entries.reduce((count, entry) => count + (entry.interfaceToolCount ?? 0), 0);
        return <article className="tool-pack-card collection" key={collection.id}><header><div><strong>{collection.name}</strong><small>{toolCount} agent capabilities · {interfaceCount || "custom"} exact tool interfaces · one isolated image</small></div><span className="signed-badge"><ShieldCheck size={12} /> Signed</span></header><p>Includes network, web, discovery, code-analysis, crypto, and utility commands with an indexed compatibility contract.</p><div className="scope-chip-list">{[...new Set(collection.entries.flatMap((entry) => entry.permissions))].map((permission) => <span key={permission}>{permission.replaceAll("_", " ")}</span>)}</div><footer><span>Installs the exact image and interface-catalog digests</span><button className="button primary" type="button" disabled={!readyRunner || complete || busy === busyId || previewMode} onClick={() => installCollection(collection.id)}>{complete ? "Installed" : busy === busyId ? "Installing…" : `Install ${collection.name}`}</button></footer></article>;
      })}</div></section>}
      <div className="tooling-grid">
        <div className="tooling-column">
          <h3>Installed environments</h3>
          {packs.length ? <div className="tool-pack-list">{packs.map((pack) => {
            const packTools = tools.filter((tool) => tool.packId === pack.id || tool.packManifestDigest === pack.manifestDigest);
            const declaredToolNames = pack.toolNames.length ? pack.toolNames : packTools.map((tool) => tool.name);
            return <article className="tool-pack-card" key={pack.id}><header><div><strong>{packLabel(pack)}</strong><code title={pack.manifestDigest}>{pack.manifestDigest.slice(0, 18)}…</code></div><span className={`pack-status ${pack.status}`}>{pack.status}</span></header><p>{declaredToolNames.length ? declaredToolNames.join(", ") : "No tools declared"}</p><div className="pack-facts"><span>{pack.trustState === "trusted" ? <ShieldCheck size={13} /> : <AlertTriangle size={13} />}{pack.trustState}</span><span>{packTools.filter((tool) => tool.available).length}/{packTools.length || pack.toolNames.length} available</span>{pack.interfaceCatalogDigest && <span title={pack.interfaceCatalogDigest}>Interface v2 · {pack.interfaceCatalogDigest.slice(0, 10)}…</span>}</div>{pack.failureDetail && <small className="form-error">{pack.failureDetail}</small>}<footer><button className="button quiet" type="button" disabled={busy === pack.id || previewMode} onClick={() => api && void action(pack.id, () => api.verifyToolPack(pack.id))}><CheckCircle2 size={13} /> Verify</button><button className="button quiet" type="button" disabled={busy === pack.id || previewMode || pack.source.startsWith("local")} title={pack.source.startsWith("local") ? "Local packs are replaced by uploading a new bundle" : undefined} onClick={() => api && void action(pack.id, () => api.updateToolPack(pack.id))}><RefreshCw size={13} /> Update</button><button className="icon-button subtle" type="button" aria-label={`Remove ${pack.name}`} disabled={busy === pack.id || previewMode} onClick={() => void removePack(pack)}><Trash2 size={13} /></button></footer></article>;
          })}</div> : <div className="empty-state compact"><Package size={22} /><strong>{loading ? "Loading installed environments…" : "No execution environment installed"}</strong><p>Install Nebula Toolbox after a verified runner is configured.</p></div>}
        </div>
        <div className="tooling-column">
          <h3>Environment catalog</h3>
          {catalog.length ? <div className="tool-pack-list">{catalog.map((entry) => <article className="tool-pack-card catalog" key={entry.id}><header><div><strong>{entry.publisher}/{entry.name}</strong><small>Version {entry.version}</small></div>{entry.signed && <span className="signed-badge"><ShieldCheck size={12} /> Signed</span>}</header><p>{entry.description || entry.toolNames.join(", ")}</p><div className="scope-chip-list">{entry.permissions.map((permission) => <span key={permission}>{permission.replaceAll("_", " ")}</span>)}</div><footer><span>{entry.platforms.join(" · ") || "Platform manifest"}</span><button className="button primary" type="button" disabled={!readyRunner || installedDigests.has(entry.manifestDigest) || busy === entry.id || previewMode} onClick={() => install(entry)}>{installedDigests.has(entry.manifestDigest) ? "Installed" : busy === entry.id ? "Installing…" : "Install"}</button></footer></article>)}</div> : <div className="empty-state compact"><Package size={22} /><strong>{loading ? "Loading catalog…" : "Catalog unavailable"}</strong><p>An installed environment can still run by digest when the signed catalog cannot be reached.</p></div>}
        </div>
      </div>
      <details className="developer-pack"><summary>Use a custom compatible environment</summary><div><p>Build or derive an OCI image that implements <code>nebula.toolbox/v1</code> and includes a complete <code>nebula.toolbox.catalog/v2</code> interface catalog. Commands without a complete interface remain available to agents through the container-only shell fallback.</p><label>Environment bundle<input type="file" accept=".nebula-toolpack,.zip,application/zip" onChange={(event: ChangeEvent<HTMLInputElement>) => setLocalBundle(event.target.files?.[0])} /></label><label className="provider-consent"><input type="checkbox" checked={developerConfirmed} onChange={(event) => setDeveloperConfirmed(event.target.checked)} /><span><strong>Enable this untrusted local environment</strong><small>Its requested permissions remain visibly marked. Local environments are never fetched from a remote URL.</small></span></label><button className="button secondary" type="button" disabled={!localBundle || !developerConfirmed || !readyRunner || busy === "local"} onClick={() => void installLocal()}><Upload size={14} /> {busy === "local" ? "Installing…" : "Install custom environment"}</button></div></details>
    </section>
  );
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
  const [egressHelperImage, setEgressHelperImage] = useState("");
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
        setSelectedId(selected.id); setSetupKind(profileSetup(selected)); setName(selected.name); setExecutable(selected.executable); setContext(selected.context ?? ""); setSocket(selected.socket ?? ""); setPlatform(selected.platform); setEgressHelperImage(selected.egressHelperImage ?? ""); setSeccompProfile(selected.seccompProfile ?? "");
      }
    } catch (loadError) {
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
    if (profile) { setSetupKind(profileSetup(profile)); setName(profile.name); setExecutable(profile.executable); setContext(profile.context ?? ""); setSocket(profile.socket ?? ""); setPlatform(profile.platform); setEgressHelperImage(profile.egressHelperImage ?? ""); setSeccompProfile(profile.seccompProfile ?? ""); }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api) return;
    setSaving(true); setError(undefined);
    try {
      const current = profiles.find((profile) => profile.id === selectedId);
      const defaults = runtimeDefaults[setupKind];
      const saved = await api.updateRunnerProfile(selectedId, { name, runtimeType: defaults.runtime, isolationMode: defaults.isolation, executable, context: context || undefined, socket: socket || undefined, platform, egressHelperImage: egressHelperImage || undefined, seccompProfile: seccompProfile || undefined, expectedRevision: current?.revision });
      setProfiles((items) => [saved, ...items.filter((profile) => profile.id !== saved.id)]);
      setSelectedId(saved.id);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Could not save the runner profile.");
    } finally { setSaving(false); }
  };

  return <section className="settings-section" id="runtime-settings"><div className="section-heading"><div><h2>Sandbox runners</h2><p>Select a trusted absolute executable and local runtime context. Nebula never discovers a runner through PATH.</p></div>{profiles.length > 1 && <label className="inline-select">Profile<select aria-label="Runner profile" value={selectedId} onChange={(event) => chooseProfile(event.target.value)}>{profiles.map((profile) => <option value={profile.id} key={profile.id}>{profile.name}</option>)}</select></label>}</div>{!available ? <div className="feature-unavailable" role="status"><Server size={22} /><div><strong>Runner profiles are not available in this Core build</strong><p>Core reports {health?.runner ?? "unavailable"}; missions remain analysis-only.</p></div></div> : <div className="runner-layout"><form className="runner-form panel" onSubmit={(event) => void save(event)}><label>Runtime<select value={setupKind} onChange={(event) => chooseRuntime(event.target.value as RunnerSetupKind)}><option value="podman_machine">Podman Machine · macOS</option><option value="docker_desktop">Docker Desktop · macOS</option><option value="podman">Rootless Podman · Linux</option><option value="docker">Rootless Docker · Linux</option></select></label><label>Profile name<input required value={name} onChange={(event) => setName(event.target.value)} /></label><label>Trusted executable<input required value={executable} pattern="/.*" spellCheck={false} onChange={(event) => setExecutable(event.target.value)} /></label><label>Container platform<select value={platform} onChange={(event) => setPlatform(event.target.value)}><option value="linux/arm64">Linux ARM64</option><option value="linux/amd64">Linux AMD64</option></select></label><label>Local context<input value={context} placeholder="Optional local runtime context" onChange={(event) => setContext(event.target.value)} /></label><label>Local Unix socket<input value={socket} pattern="^$|(?:unix://)?/.*" placeholder="Optional absolute Unix socket path" spellCheck={false} onChange={(event) => setSocket(event.target.value)} /></label><label>Egress helper override<input value={egressHelperImage} placeholder="Optional digest-pinned custom helper image" spellCheck={false} onChange={(event) => setEgressHelperImage(event.target.value)} /></label><label>Seccomp profile<input value={seccompProfile} pattern="^$|/.*" placeholder="Optional absolute local profile path" spellCheck={false} onChange={(event) => setSeccompProfile(event.target.value)} /></label>{error && <p className="form-error" role="alert">{error}</p>}<footer><span>No remote TCP endpoints or automatic runtime installation.</span><button className="button primary" type="submit" disabled={saving || previewMode}>{saving ? "Checking…" : "Save and check"}</button></footer></form><aside className="panel runner-status"><header className="panel-header compact"><div><h3>Isolation status</h3><p>{runtime?.mode ?? "desktop"} control plane</p></div><Server size={18} /></header>{profiles.length ? profiles.map((profile) => <article key={profile.id}><span className={`status-dot ${profile.state === "ready" ? "healthy" : "unavailable"}`} /><div><strong>{profile.name}</strong><small>{profile.state} · {profile.isolationMode.replaceAll("_", " ")} · {profile.platform}</small>{profile.detail && <p>{profile.detail}</p>}<p>{profile.egressHelperImage ? "This profile overrides the environment's embedded egress helper." : "Compatible Toolbox environments supply their own digest-pinned egress helper."}</p></div></article>) : <div className="empty-state compact"><Server size={21} /><strong>No explicit runner profile</strong><p>Save this profile to ask Core to verify the local runtime and isolation boundary.</p></div>}</aside></div>}</section>;
}
