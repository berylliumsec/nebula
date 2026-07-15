import { useEffect, useState, type FormEvent } from "react";
import { Bot, Network, Pencil, Plus, RefreshCw, ShieldAlert, Trash2, X } from "lucide-react";
import type { HarnessProfile, McpServerProfile } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

const approvalOptions = ["risk_based", "ask", "allow", "deny"] as const;

export function HarnessSettings() {
  const { api, coreState, engagement, previewMode } = useWorkspace();
  const [harnesses, setHarnesses] = useState<HarnessProfile[]>([]);
  const [servers, setServers] = useState<McpServerProfile[]>([]);
  const [harnessDialog, setHarnessDialog] = useState(false);
  const [mcpDialog, setMcpDialog] = useState(false);
  const [editingHarness, setEditingHarness] = useState<HarnessProfile>();
  const [editingServer, setEditingServer] = useState<McpServerProfile>();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<HarnessProfile["kind"]>("codex_app_server");
  const [connectionMode, setConnectionMode] = useState<HarnessProfile["connectionMode"]>("spawn");
  const [transport, setTransport] = useState<HarnessProfile["transport"]>("stdio");
  const [executable, setExecutable] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [model, setModel] = useState("");
  const [harnessAuthMode, setHarnessAuthMode] = useState<HarnessProfile["authMode"]>("existing_session");
  const [harnessSecret, setHarnessSecret] = useState("");
  const [harnessSessionCredential, setHarnessSessionCredential] = useState(false);
  const [harnessLocalOnly, setHarnessLocalOnly] = useState(false);
  const [harnessSensitiveData, setHarnessSensitiveData] = useState(false);
  const [mcpName, setMcpName] = useState("");
  const [mcpTransport, setMcpTransport] = useState<McpServerProfile["transport"]>("stdio");
  const [command, setCommand] = useState("");
  const [argumentsText, setArgumentsText] = useState("");
  const [url, setUrl] = useState("");
  const [trusted, setTrusted] = useState(false);
  const [required, setRequired] = useState(false);
  const [defaultApproval, setDefaultApproval] = useState<McpServerProfile["defaultApproval"]>("risk_based");
  const [mcpAuthMode, setMcpAuthMode] = useState<McpServerProfile["authMode"]>("none");
  const [mcpSecret, setMcpSecret] = useState("");
  const [mcpHeaderName, setMcpHeaderName] = useState("X-API-Key");
  const [sessionCredential, setSessionCredential] = useState(false);
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();

  const reload = async () => {
    if (!api || coreState !== "online") return;
    const [nextHarnesses, nextServers] = await Promise.all([api.listHarnesses(), api.listMcpServers()]);
    setHarnesses(nextHarnesses);
    setServers(nextServers);
  };

  useEffect(() => {
    let active = true;
    if (!api || coreState !== "online") return () => { active = false; };
    void Promise.all([api.listHarnesses(), api.listMcpServers()])
      .then(([nextHarnesses, nextServers]) => {
        if (!active) return;
        setHarnesses(nextHarnesses);
        setServers(nextServers);
      })
      .catch((loadError) => {
        void logCaughtDiagnostic("interface.harness_settings.caught_failure_01", "A handled interface operation failed.", loadError, "harness_settings");
        if (active) setError(loadError instanceof Error ? loadError.message : "Harness settings are unavailable.");
      });
    return () => { active = false; };
  }, [api, coreState]);

  const openHarness = (profile?: HarnessProfile) => {
    setEditingHarness(profile);
    setName(profile?.name ?? "");
    setKind(profile?.kind ?? "codex_app_server");
    setConnectionMode(profile?.connectionMode ?? "spawn");
    setTransport(profile?.transport ?? "stdio");
    setExecutable(profile?.executable ?? "");
    setEndpoint(profile?.endpoint ?? "");
    setModel(profile?.defaultModel ?? "");
    setHarnessAuthMode(profile?.authMode ?? "existing_session");
    setHarnessSecret("");
    setHarnessSessionCredential(false);
    setHarnessLocalOnly(profile?.localOnly ?? false);
    setHarnessSensitiveData(profile?.permitsSensitiveData ?? false);
    setError(undefined);
    setHarnessDialog(true);
  };

  const submitHarness = async (event: FormEvent) => {
    event.preventDefault();
    if (!api) return;
    setBusy(editingHarness?.id ?? "new-harness");
    setError(undefined);
    try {
      let secretRef = editingHarness?.secretRef;
      if (harnessAuthMode !== "existing_session" && harnessSecret) {
        secretRef = (await api.createCredential(harnessSecret, harnessSessionCredential ? "session" : "vault")).reference;
      }
      if (harnessAuthMode !== "existing_session" && !secretRef) {
        throw new Error("This authentication mode requires a write-only credential.");
      }
      const payload = {
        name: name.trim(),
        kind,
        connection_mode: connectionMode,
        transport,
        executable: connectionMode === "spawn" && executable.trim() ? executable.trim() : null,
        endpoint: connectionMode === "endpoint" ? endpoint.trim() : null,
        auth_mode: harnessAuthMode,
        secret_ref: harnessAuthMode === "existing_session" ? null : secretRef,
        default_model: model.trim() || null,
        enabled: editingHarness?.enabled ?? true,
        privacy: {
          local_only: harnessLocalOnly,
          permits_sensitive_data: harnessSensitiveData,
        },
      };
      if (editingHarness) await api.updateHarness(editingHarness.id, payload, editingHarness.revision);
      else await api.createHarness(payload);
      await reload();
      setHarnessDialog(false);
    } catch (saveError) {
      void logCaughtDiagnostic("interface.harness_settings.caught_failure_02", "A handled interface operation failed.", saveError, "harness_settings");
      setError(saveError instanceof Error ? saveError.message : "Could not save the harness profile.");
    } finally {
      setBusy(undefined);
    }
  };

  const openMcp = (server?: McpServerProfile) => {
    setEditingServer(server);
    setMcpName(server?.name ?? "");
    setMcpTransport(server?.transport ?? "stdio");
    setCommand(server?.command ?? "");
    setArgumentsText(server?.arguments.join("\n") ?? "");
    setUrl(server?.url ?? "");
    setTrusted(server?.trustedStdio ?? false);
    setRequired(server?.required ?? false);
    setDefaultApproval(server?.defaultApproval ?? "risk_based");
    setMcpAuthMode(server?.authMode ?? "none");
    setMcpSecret("");
    setMcpHeaderName("X-API-Key");
    setError(undefined);
    setMcpDialog(true);
  };

  const submitMcp = async (event: FormEvent) => {
    event.preventDefault();
    if (!api) return;
    setBusy(editingServer?.id ?? "new-mcp");
    setError(undefined);
    const isStdio = mcpTransport === "stdio";
    try {
      let credentialRef: string | undefined;
      if (!isStdio && mcpAuthMode !== "none" && mcpSecret) {
        credentialRef = (await api.createCredential(mcpSecret, sessionCredential ? "session" : "vault")).reference;
      }
      if (!isStdio && mcpAuthMode !== "none" && !editingServer && !credentialRef) {
        throw new Error("Bearer and header authentication require a write-only credential.");
      }
      const payload = {
      name: mcpName.trim(),
      transport: mcpTransport,
      command: isStdio ? command.trim() : null,
      arguments: isStdio ? argumentsText.split("\n").map((item) => item.trim()).filter(Boolean) : [],
      url: isStdio ? null : url.trim(),
      auth_mode: isStdio ? "none" : mcpAuthMode,
      ...(isStdio || mcpAuthMode === "none" ? { bearer_secret_ref: null, header_secret_refs: {} } : {}),
      ...(credentialRef && mcpAuthMode === "bearer" ? { bearer_secret_ref: credentialRef } : {}),
      ...(credentialRef && mcpAuthMode === "bearer" ? { header_secret_refs: {} } : {}),
      ...(credentialRef && mcpAuthMode === "headers" ? { bearer_secret_ref: null, header_secret_refs: { [mcpHeaderName.trim()]: credentialRef } } : {}),
      cwd_policy: "workspace",
      enabled: editingServer?.enabled ?? false,
      required,
      trusted_stdio: isStdio && trusted,
      default_approval: defaultApproval,
      };
      if (editingServer) await api.updateMcpServer(editingServer.id, payload, editingServer.revision);
      else await api.createMcpServer(payload);
      await reload();
      setMcpDialog(false);
    } catch (saveError) {
      void logCaughtDiagnostic("interface.harness_settings.caught_failure_03", "A handled interface operation failed.", saveError, "harness_settings");
      setError(saveError instanceof Error ? saveError.message : "Could not save the MCP server.");
    } finally {
      setBusy(undefined);
    }
  };

  const updateHarness = async (profile: HarnessProfile, changes: Record<string, unknown>) => {
    if (!api) return;
    setBusy(profile.id);
    try {
      await api.updateHarness(profile.id, changes, profile.revision);
      await reload();
    } catch (actionError) {
      void logCaughtDiagnostic("interface.harness_settings.caught_failure_04", "A handled interface operation failed.", actionError, "harness_settings");
      setError(actionError instanceof Error ? actionError.message : "Harness action failed.");
    } finally { setBusy(undefined); }
  };

  const updateServer = async (server: McpServerProfile, changes: Record<string, unknown>) => {
    if (!api) return;
    setBusy(server.id);
    try {
      await api.updateMcpServer(server.id, changes, server.revision);
      await reload();
    } catch (actionError) {
      void logCaughtDiagnostic("interface.harness_settings.caught_failure_05", "A handled interface operation failed.", actionError, "harness_settings");
      setError(actionError instanceof Error ? actionError.message : "MCP server action failed.");
    } finally { setBusy(undefined); }
  };

  return <>
    <section className="settings-section" id="harness-settings">
      <div className="section-heading"><div><h2>Agent harnesses</h2><p>Stateful Codex and Claude runtimes shared by chat and missions.</p></div><button className="button primary" type="button" disabled={previewMode} onClick={() => openHarness()}><Plus size={16} /> Add harness</button></div>
      {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." />}
      {harnesses.length ? <div className="provider-grid">{harnesses.map((profile) => <article className="panel provider-card" key={profile.id}>
        <header><span className={`status-dot ${profile.enabled ? profile.healthy ? "healthy" : "warning" : "unavailable"}`} /><div><small>{profile.kind === "codex_app_server" ? "Codex App Server" : "Claude Agent SDK"}</small><h3>{profile.name}</h3></div></header>
        <p>{profile.detail ?? `${profile.connectionMode} · ${profile.transport}${profile.transport === "websocket" ? " · experimental" : ""}`}</p>
        <dl><div><dt>Model</dt><dd>{profile.defaultModel ?? "Selected per session"}</dd></div><div><dt>Auth</dt><dd>{profile.authMode === "existing_session" ? "Existing local sign-in" : "Secret-backed · configured"}</dd></div><div><dt>Privacy</dt><dd>{profile.localOnly ? "Local runtime" : profile.permitsSensitiveData ? "Cloud data allowed with confirmation" : "Text only"}</dd></div><div><dt>Version</dt><dd>{profile.version ?? "Not checked"}</dd></div></dl>
        <footer><button className="button quiet" type="button" disabled={busy === profile.id} onClick={() => { setBusy(profile.id); void api?.checkHarness(profile.id).then(reload).catch((actionError) => { void logCaughtDiagnostic("interface.harness_settings.caught_failure_06", "A handled interface operation failed.", actionError, "harness_settings"); return setError(actionError instanceof Error ? actionError.message : "Health check failed."); }).finally(() => setBusy(undefined)); }}><RefreshCw className={busy === profile.id ? "spin" : undefined} size={14} /> Check</button><button className="icon-button subtle" aria-label={`Edit ${profile.name}`} type="button" onClick={() => openHarness(profile)}><Pencil size={14} /></button><button className="button quiet" type="button" disabled={busy === profile.id} onClick={() => void updateHarness(profile, { enabled: !profile.enabled })}>{profile.enabled ? "Disable" : "Enable"}</button><button className="icon-button subtle" aria-label={`Delete ${profile.name}`} type="button" disabled={busy === profile.id} onClick={() => { setBusy(profile.id); void api?.deleteHarness(profile.id, profile.revision).then(reload).catch((actionError) => { void logCaughtDiagnostic("interface.harness_settings.caught_failure_07", "A handled interface operation failed.", actionError, "harness_settings"); return setError(actionError instanceof Error ? actionError.message : "Delete failed."); }).finally(() => setBusy(undefined)); }}><Trash2 size={14} /></button></footer>
      </article>)}</div> : <div className="empty-state compact"><Bot size={23} /><strong>No agent harnesses</strong><p>Add Codex App Server or Claude Agent SDK when you want vendor-managed sessions.</p></div>}
    </section>
    <section className="settings-section" id="mcp-settings">
      <div className="section-heading"><div><h2>MCP servers</h2><p>Core-owned profiles available to native agents, missions, Codex, and Claude. Schemas are frozen per turn/run and every result is captured as an artifact.</p></div><button className="button primary" type="button" disabled={previewMode} onClick={() => openMcp()}><Plus size={16} /> Add MCP server</button></div>
      {servers.length ? <div className="provider-grid">{servers.map((server) => <article className="panel provider-card" key={server.id}>
        <header><span className={`status-dot ${server.enabled ? server.checkedAt ? "healthy" : "warning" : "unavailable"}`} /><div><small>{server.transport === "stdio" ? "Trusted local program" : "Streamable HTTP"}</small><h3>{server.name}</h3></div></header>
        {server.transport === "stdio" && <p className="provider-dialog-note"><ShieldAlert size={14} /> Runs outside the Toolbox boundary. Enable only after trusting this executable.</p>}
        <p>{server.detail ?? `${server.tools.length} discovered tool${server.tools.length === 1 ? "" : "s"} · ${server.defaultApproval.replace("_", " ")}`}</p>
        {server.tools.length > 0 && <div className="mcp-tool-policies">{server.tools.map((tool) => <label key={tool.name}><span><strong>{tool.name}</strong><small>{tool.readOnly ? "read-only" : "write/unknown"}{tool.destructive ? " · destructive" : ""}{tool.openWorld ? " · open-world" : ""}</small></span><select aria-label={`${tool.name} approval policy`} value={tool.approval} onChange={(event) => void updateServer(server, { tool_overrides: { ...server.toolOverrides, [tool.name]: event.target.value } })}>{approvalOptions.map((option) => <option value={option} key={option}>{option.replace("_", " ")}</option>)}</select></label>)}</div>}
        <footer><button className="button quiet" type="button" disabled={busy === server.id} onClick={() => { setBusy(server.id); void api?.probeMcpServer(server.id, engagement?.id).then(reload).catch((actionError) => { void logCaughtDiagnostic("interface.harness_settings.caught_failure_08", "A handled interface operation failed.", actionError, "harness_settings"); return setError(actionError instanceof Error ? actionError.message : "Probe failed."); }).finally(() => setBusy(undefined)); }}><RefreshCw className={busy === server.id ? "spin" : undefined} size={14} /> Probe</button><button className="icon-button subtle" aria-label={`Edit ${server.name}`} type="button" onClick={() => openMcp(server)}><Pencil size={14} /></button><button className="button quiet" type="button" disabled={busy === server.id || (server.transport === "stdio" && !server.trustedStdio)} onClick={() => void updateServer(server, { enabled: !server.enabled })}>{server.enabled ? "Disable" : "Enable"}</button><button className="icon-button subtle" aria-label={`Delete ${server.name}`} type="button" disabled={busy === server.id} onClick={() => { setBusy(server.id); void api?.deleteMcpServer(server.id, server.revision).then(reload).catch((actionError) => { void logCaughtDiagnostic("interface.harness_settings.caught_failure_09", "A handled interface operation failed.", actionError, "harness_settings"); return setError(actionError instanceof Error ? actionError.message : "Delete failed."); }).finally(() => setBusy(undefined)); }}><Trash2 size={14} /></button></footer>
      </article>)}</div> : <div className="empty-state compact"><Network size={23} /><strong>No MCP server profiles</strong><p>Profiles are never launched until an explicit probe or selected agent runtime uses them.</p></div>}
    </section>
    {harnessDialog && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="harness-dialog-title" onSubmit={(event) => void submitHarness(event)}><header><div><small>Agent harness</small><h2 id="harness-dialog-title">{editingHarness ? "Edit harness" : "Add harness"}</h2></div><button className="icon-button subtle" type="button" aria-label="Close harness dialog" onClick={() => setHarnessDialog(false)}><X size={17} /></button></header><label>Name<input required value={name} onChange={(event) => setName(event.target.value)} /></label><label>Harness<select value={kind} disabled={Boolean(editingHarness)} onChange={(event) => { const next = event.target.value as HarnessProfile["kind"]; setKind(next); if (next === "claude_agent_sdk") { setConnectionMode("spawn"); setTransport("stdio"); if (harnessAuthMode === "endpoint_bearer") setHarnessAuthMode("existing_session"); } }}><option value="codex_app_server">Codex App Server</option><option value="claude_agent_sdk">Claude Agent SDK</option></select></label>{kind === "codex_app_server" && <label>Connection<select value={connectionMode} onChange={(event) => { const next = event.target.value as HarnessProfile["connectionMode"]; setConnectionMode(next); setTransport(next === "spawn" ? "stdio" : "unix"); setHarnessAuthMode("existing_session"); setHarnessSecret(""); }}><option value="spawn">Managed process</option><option value="endpoint">Pre-launched endpoint</option></select></label>}{connectionMode === "spawn" ? <label>Absolute executable path<input required={kind === "codex_app_server"} value={executable} placeholder="/usr/local/bin/codex" onChange={(event) => setExecutable(event.target.value)} /></label> : <><label>Transport<select value={transport} onChange={(event) => setTransport(event.target.value as HarnessProfile["transport"])}><option value="unix">Unix socket</option><option value="websocket">Loopback WebSocket (experimental)</option></select></label><label>Endpoint<input required value={endpoint} placeholder={transport === "unix" ? "unix:///path/to/socket" : "ws://127.0.0.1:4500"} onChange={(event) => setEndpoint(event.target.value)} /></label></>}<label>Authentication<select value={harnessAuthMode} onChange={(event) => { setHarnessAuthMode(event.target.value as HarnessProfile["authMode"]); setHarnessSecret(""); }}><option value="existing_session">Existing vendor sign-in</option><option value={connectionMode === "endpoint" ? "endpoint_bearer" : "secret_ref"}>{connectionMode === "endpoint" ? "Endpoint bearer token" : "API key credential"}</option></select></label>{harnessAuthMode !== "existing_session" && <><label>{editingHarness?.secretRef ? "Replacement credential" : "Credential"}<input type="password" autoComplete="new-password" value={harnessSecret} placeholder={editingHarness?.secretRef ? "Leave blank to keep current authentication" : "Write-only secret"} onChange={(event) => setHarnessSecret(event.target.value)} /></label><label className="provider-consent"><input type="checkbox" checked={harnessSessionCredential} onChange={(event) => setHarnessSessionCredential(event.target.checked)} /><span><strong>Use for this session only</strong><small>Otherwise Core stores it in the operating-system credential vault.</small></span></label></>}<label>Default model<input value={model} placeholder="Optional; users can choose per session" onChange={(event) => setModel(event.target.value)} /></label><label className="provider-consent"><input type="checkbox" checked={harnessLocalOnly} onChange={(event) => setHarnessLocalOnly(event.target.checked)} /><span><strong>Model runtime is local</strong><small>Only enable when prompts and outputs do not leave this machine.</small></span></label><label className="provider-consent"><input type="checkbox" checked={harnessSensitiveData} onChange={(event) => setHarnessSensitiveData(event.target.checked)} /><span><strong>Permit project/document data</strong><small>Non-local sessions still require confirmation on each knowledge-bearing request.</small></span></label><p className="provider-dialog-note">Nebula launches the absolute executable directly—never through a shell or ambient PATH search. Secret values are never returned after submission.</p>{error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}<footer><button className="button secondary" type="button" onClick={() => setHarnessDialog(false)}>Cancel</button><button className="button primary" type="submit" disabled={Boolean(busy) || !name.trim()}>{busy ? "Saving…" : "Save harness"}</button></footer></form></div>}
    {mcpDialog && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="mcp-dialog-title" onSubmit={(event) => void submitMcp(event)}><header><div><small>MCP registry</small><h2 id="mcp-dialog-title">{editingServer ? "Edit MCP server" : "Add MCP server"}</h2></div><button className="icon-button subtle" type="button" aria-label="Close MCP dialog" onClick={() => setMcpDialog(false)}><X size={17} /></button></header><label>Name<input required pattern="[A-Za-z0-9._-]+" value={mcpName} onChange={(event) => setMcpName(event.target.value)} /></label><label>Transport<select value={mcpTransport} onChange={(event) => setMcpTransport(event.target.value as McpServerProfile["transport"])}><option value="stdio">stdio</option><option value="streamable_http">Streamable HTTP</option></select></label>{mcpTransport === "stdio" ? <><label>Absolute command<input required value={command} placeholder="/usr/local/bin/my-mcp-server" onChange={(event) => setCommand(event.target.value)} /></label><label>Arguments<textarea rows={3} value={argumentsText} placeholder="One literal argument per line" onChange={(event) => setArgumentsText(event.target.value)} /></label><label className="provider-consent"><input type="checkbox" checked={trusted} onChange={(event) => setTrusted(event.target.checked)} /><span><strong>I trust this local program</strong><small>Probing or using it executes outside Nebula's containerized Toolbox boundary.</small></span></label></> : <><label>HTTPS endpoint<input required type="url" value={url} placeholder="https://mcp.example.com/mcp" onChange={(event) => setUrl(event.target.value)} /></label><label>Authentication<select value={mcpAuthMode} onChange={(event) => setMcpAuthMode(event.target.value as McpServerProfile["authMode"])}><option value="none">Unauthenticated</option><option value="bearer">Bearer token</option><option value="headers">Secret header</option></select></label>{mcpAuthMode !== "none" && <><label>{editingServer ? "Replacement credential" : "Credential"}<input type="password" autoComplete="new-password" value={mcpSecret} placeholder={editingServer ? "Leave blank to keep current authentication" : "Write-only secret"} onChange={(event) => setMcpSecret(event.target.value)} /></label>{mcpAuthMode === "headers" && <label>Header name<input required value={mcpHeaderName} onChange={(event) => setMcpHeaderName(event.target.value)} /></label>}<label className="provider-consent"><input type="checkbox" checked={sessionCredential} onChange={(event) => setSessionCredential(event.target.checked)} /><span><strong>Use for this session only</strong><small>Otherwise Core stores it in the operating-system credential vault.</small></span></label></>}</>}<label>Default approval<select value={defaultApproval} onChange={(event) => setDefaultApproval(event.target.value as McpServerProfile["defaultApproval"])}>{approvalOptions.map((option) => <option value={option} key={option}>{option.replace("_", " ")}</option>)}</select></label><label className="provider-consent"><input type="checkbox" checked={required} onChange={(event) => setRequired(event.target.checked)} /><span><strong>Required server</strong><small>Fail new sessions when this server cannot initialize.</small></span></label><p className="provider-dialog-note">OAuth and interactive elicitation are not supported in this release. Secret values are never returned after submission.</p>{error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}<footer><button className="button secondary" type="button" onClick={() => setMcpDialog(false)}>Cancel</button><button className="button primary" type="submit" disabled={Boolean(busy) || !mcpName.trim()}>{busy ? "Saving…" : "Save MCP server"}</button></footer></form></div>}
  </>;
}
