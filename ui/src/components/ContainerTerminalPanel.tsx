import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import {
  Box,
  CircleStop,
  ExternalLink,
  LoaderCircle,
  Network,
  RotateCcw,
  ShieldCheck,
  SquareTerminal,
  WifiOff,
  X,
} from "lucide-react";
import type { ApiClient } from "../api/client";
import { ContainerTerminalSocket, type ContainerTerminalSocketState } from "../api/containerTerminal";
import type {
  ContainerTerminalCapabilities,
  ContainerTerminalPreflight,
  ContainerTerminalRequest,
  ContainerTerminalSession,
} from "../api/types";

interface ContainerTerminalPanelProps {
  api: ApiClient;
  engagementId: string;
  engagementName: string;
}

function idempotencyKey(): string {
  return `container-terminal-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
}

function parsePorts(value: string): number[] | undefined {
  const parts = value.split(/[\s,]+/).filter(Boolean);
  if (!parts.length) return undefined;
  const ports = parts.map(Number);
  if (ports.some((port) => !Number.isInteger(port) || port < 1 || port > 65_535)) return undefined;
  return [...new Set(ports)].sort((left, right) => left - right);
}

function commandLabel(preflight: ContainerTerminalPreflight): string {
  if (!preflight.runtime) return "Unavailable";
  return [preflight.runtime.interpreter, ...preflight.runtime.arguments].join(" ");
}

function LiveContainerTerminal({
  api,
  session,
  networkLabel,
  onAnother,
}: {
  api: ApiClient;
  session: ContainerTerminalSession;
  networkLabel: string;
  onAnother: () => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const socketRef = useRef<ContainerTerminalSocket | undefined>(undefined);
  const [state, setState] = useState<ContainerTerminalSocketState>("connecting");
  const [error, setError] = useState<string>();
  const [exit, setExit] = useState<{ outcome: string; exitCode?: number }>();

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const terminal = new Terminal({
      cursorBlink: true,
      cursorStyle: "bar",
      fontFamily: '"Noto Sans Mono", "SFMono-Regular", Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.25,
      scrollback: 10_000,
      theme: {
        background: "#071017",
        foreground: "#d9e5e9",
        cursor: "#54d6a3",
        selectionBackground: "#245f5588",
        black: "#071017",
        brightBlack: "#53656d",
        green: "#54d6a3",
        brightGreen: "#7ce9bd",
        red: "#ff7f86",
        brightRed: "#ff9da3",
        yellow: "#e3c877",
        brightYellow: "#f0d990",
        blue: "#7bbcf2",
        brightBlue: "#a1d1f7",
      },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(host);
    host.querySelector("textarea")?.setAttribute("aria-label", "Container terminal input");

    const socket = new ContainerTerminalSocket({
      apiBaseUrl: api.baseUrl,
      token: api.getToken(),
      session,
      onState: setState,
      onOutput: (data) => terminal.write(data),
      onReady: () => {
        setError(undefined);
        terminal.focus();
      },
      onError: (_code, detail) => setError(detail),
      onExit: (result) => setExit(result),
    });
    socketRef.current = socket;
    const input = terminal.onData((data) => socket.sendInput(data));
    const resize = terminal.onResize(({ cols, rows }) => socket.resize(cols, rows));
    const fitTerminal = () => {
      try {
        fit.fit();
        if (terminal.cols > 0 && terminal.rows > 0) socket.resize(terminal.cols, terminal.rows);
      } catch {
        // The view can briefly have zero dimensions while tabs are changing.
      }
    };
    const frame = globalThis.requestAnimationFrame?.(fitTerminal);
    const observer = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(fitTerminal);
    observer?.observe(host);
    globalThis.addEventListener("resize", fitTerminal);
    socket.connect();
    return () => {
      if (frame !== undefined) globalThis.cancelAnimationFrame?.(frame);
      observer?.disconnect();
      globalThis.removeEventListener("resize", fitTerminal);
      input.dispose();
      resize.dispose();
      socket.dispose();
      socketRef.current = undefined;
      terminal.dispose();
    };
  }, [api, session]);

  const statusLabel = exit
    ? exit.outcome.replaceAll("_", " ")
    : state === "ready"
      ? "Connected"
      : state === "connecting"
        ? "Starting container…"
        : state.replaceAll("_", " ");

  return <div className="container-terminal-live">
    <header>
      <div><span className={`status-dot ${state === "ready" ? "healthy" : state === "error" ? "unavailable" : "warning"}`} /><span><strong>{statusLabel}</strong><small>{networkLabel} · disposable Toolbox container · /workspace</small></span></div>
      {exit ? <button className="button secondary" type="button" onClick={onAnother}><RotateCcw size={15} /> New terminal</button> : <button className="button danger" type="button" disabled={state === "closing" || state === "closed"} onClick={() => socketRef.current?.requestClose()}><CircleStop size={15} /> Stop terminal</button>}
    </header>
    {error && <p className="terminal-error" role="alert">{error}</p>}
    <div className="xterm-shell" ref={hostRef} aria-label="Container terminal output" />
    <footer><ShieldCheck size={14} /> This terminal is inside the pinned container. Leaving this view closes and removes it.{exit?.exitCode !== undefined ? ` Exit code ${exit.exitCode}.` : ""}</footer>
  </div>;
}

export function ContainerTerminalPanel({ api, engagementId, engagementName }: ContainerTerminalPanelProps) {
  const [capabilities, setCapabilities] = useState<ContainerTerminalCapabilities>();
  const [loadingCapabilities, setLoadingCapabilities] = useState(true);
  const [mode, setMode] = useState<"none" | "scoped">("none");
  const [target, setTarget] = useState("");
  const [ports, setPorts] = useState("443");
  const [request, setRequest] = useState<ContainerTerminalRequest>();
  const [preview, setPreview] = useState<ContainerTerminalPreflight>();
  const [clientKey, setClientKey] = useState("");
  const [session, setSession] = useState<ContainerTerminalSession>();
  const [reviewing, setReviewing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    const controller = new AbortController();
    setLoadingCapabilities(true);
    void api.containerTerminalCapabilities(engagementId, controller.signal)
      .then((value) => {
        setCapabilities(value);
        if (!value.scopedNetwork) setMode("none");
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not inspect terminal capability.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoadingCapabilities(false);
      });
    return () => controller.abort();
  }, [api, engagementId]);

  useEffect(() => {
    setRequest(undefined);
    setPreview(undefined);
    setSession(undefined);
    setClientKey("");
    setError(undefined);
  }, [engagementId]);

  const parsedPorts = useMemo(() => parsePorts(ports), [ports]);

  const review = async (event: FormEvent) => {
    event.preventDefault();
    if (!capabilities?.ready || reviewing) return;
    if (mode === "scoped" && !target.trim()) {
      setError("Enter the single scope-approved target for this terminal.");
      return;
    }
    if (mode === "scoped" && !parsedPorts) {
      setError("Enter one or more ports from 1 to 65535.");
      return;
    }
    const next: ContainerTerminalRequest = {
      engagementId,
      network: mode === "none"
        ? { mode: "none", ports: [] }
        : { mode: "scoped", target: target.trim(), ports: parsedPorts ?? [] },
      columns: 100,
      rows: 30,
    };
    setReviewing(true);
    setError(undefined);
    try {
      const result = await api.preflightContainerTerminal(next);
      if (!result.allowed || !result.previewToken || !result.previewFingerprint || !result.runtime || !result.network) {
        setError(result.detail || "Core denied the container terminal preview.");
        return;
      }
      setRequest(next);
      setPreview(result);
      setClientKey(idempotencyKey());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not review the container terminal.");
    } finally {
      setReviewing(false);
    }
  };

  const start = async () => {
    if (!request || !preview?.previewToken || !preview.previewFingerprint || !clientKey || starting) return;
    setStarting(true);
    setError(undefined);
    try {
      const created = await api.startContainerTerminal(request, preview, clientKey);
      setSession(created);
      setPreview(undefined);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start the container terminal.");
    } finally {
      setStarting(false);
    }
  };

  if (session) {
    const networkLabel = request?.network.mode === "scoped"
      ? `Scoped to ${request.network.target}:${request.network.ports.join(",")}`
      : "Offline";
    return <LiveContainerTerminal api={api} session={session} networkLabel={networkLabel} onAnother={() => {
      setSession(undefined);
      setRequest(undefined);
      setClientKey("");
      setError(undefined);
    }} />;
  }

  return <div className="container-terminal-panel">
    <section className="container-terminal-intro">
      <span className="terminal-hero-icon"><SquareTerminal size={23} /></span>
      <div><small>Human-operated shell</small><h2>Container terminal</h2><p>Open an interactive bash session in a fresh, pinned Toolbox container for <strong>{engagementName}</strong>. The only persistent mount is this engagement’s <code>/workspace</code>.</p></div>
      <span className="terminal-boundary"><ShieldCheck size={15} /> No host shell</span>
    </section>

    <div className="container-terminal-grid">
      <form className="terminal-launch-form" onSubmit={(event) => void review(event)}>
        <header><div><Box size={17} /><span><strong>Session boundary</strong><small>Review is required before every terminal.</small></span></div></header>
        <label>Network mode<select value={mode} disabled={Boolean(preview)} onChange={(event) => { setMode(event.target.value as "none" | "scoped"); setError(undefined); }}><option value="none">Offline (recommended)</option><option value="scoped" disabled={!capabilities?.scopedNetwork}>One approved target</option></select></label>
        {mode === "scoped" && <div className="terminal-network-fields"><label>Exact target<input value={target} disabled={Boolean(preview)} placeholder="app.example.com or 10.0.0.5" onChange={(event) => { setTarget(event.target.value); setError(undefined); }} /></label><label>Allowed ports<input value={ports} disabled={Boolean(preview)} placeholder="443, 8443" onChange={(event) => { setPorts(event.target.value); setError(undefined); }} /></label></div>}
        <div className="terminal-mode-summary">{mode === "none" ? <><WifiOff size={16} /><span><strong>No network namespace</strong><small>Docker/Podman launches with <code>--network=none</code>.</small></span></> : <><Network size={16} /><span><strong>Certified scoped egress</strong><small>DNS is resolved, pinned, and limited to the selected ports.</small></span></>}</div>
        {loadingCapabilities ? <p className="terminal-capability"><LoaderCircle className="spin" size={15} /> Checking the assigned Toolbox runtime…</p> : !capabilities?.ready ? <p className="terminal-capability unavailable" role="alert"><X size={15} /> {capabilities?.detail ?? "Assign a ready environment.shell_local Toolbox capability to use the terminal."}</p> : <p className="terminal-capability"><ShieldCheck size={15} /> Assigned bash runtime is ready{capabilities.scopedNetwork ? " for offline and scoped sessions" : " for offline sessions"}.</p>}
        {error && <p className="terminal-error" role="alert">{error}</p>}
        {!preview && <button className="button primary" type="submit" disabled={!capabilities?.ready || reviewing}>{reviewing ? <><LoaderCircle className="spin" size={15} /> Reviewing…</> : <><ShieldCheck size={15} /> Review container terminal</>}</button>}
      </form>

      {preview?.runtime && preview.network ? <section className="terminal-review" aria-label="Container terminal review">
        <header><div><ShieldCheck size={18} /><span><small>Core-validated preview</small><h3>Confirm exact terminal boundary</h3></span></div><button className="icon-button subtle" type="button" aria-label="Cancel terminal review" disabled={starting} onClick={() => { setPreview(undefined); setRequest(undefined); setClientKey(""); }}><X size={16} /></button></header>
        <dl>
          <div><dt>Lifecycle</dt><dd>Fresh disposable container; removed when disconnected</dd></div>
          <div><dt>Shell</dt><dd><code>{commandLabel(preview)}</code></dd></div>
          <div><dt>Toolbox image</dt><dd title={preview.runtime.image}><code>{preview.runtime.image}</code></dd></div>
          <div><dt>Manifest</dt><dd><code>{preview.runtime.manifestDigest}</code> · {preview.runtime.trusted ? "signed/trusted" : "unsigned developer mode"}</dd></div>
          <div><dt>Runner</dt><dd>{preview.runtime.runnerRuntime} · {preview.runtime.runnerIsolation.replaceAll("_", " ")} · {preview.runtime.runnerPlatform}</dd></div>
          <div><dt>Workspace</dt><dd><code>/workspace</code> read/write · no arbitrary mounts</dd></div>
          <div><dt>Limits</dt><dd>{preview.limits.cpuCount} CPU · {preview.limits.memoryMb} MiB · {preview.limits.pids} PIDs · {Math.round(preview.limits.timeoutSeconds / 60)} min hard / {Math.round(preview.idleTimeoutSeconds / 60)} min idle</dd></div>
          <div><dt>Host access</dt><dd>None; no host shell or runtime socket in the webview</dd></div>
          <div><dt>Network</dt><dd>{preview.network.mode === "none" ? "Offline (--network=none)" : <>{preview.network.target} · ports {preview.network.ports.join(", ")}<br /><small>Pinned IPs: {preview.network.resolvedAddresses.join(", ")}</small></>}</dd></div>
          <div><dt>Policy</dt><dd>{preview.policyRule ?? "allowed"} · revision {preview.scopePolicyRevision}</dd></div>
        </dl>
        <p><ExternalLink size={14} /> The shell can run commands allowed by this fixed container/network boundary. Commands and interactive output are not sent to an AI provider.</p>
        <footer><button className="button secondary" type="button" disabled={starting} onClick={() => { setPreview(undefined); setRequest(undefined); setClientKey(""); }}>Cancel</button><button className="button primary" type="button" disabled={starting} onClick={() => void start()}>{starting ? <><LoaderCircle className="spin" size={15} /> Starting…</> : <><SquareTerminal size={15} /> Start container terminal</>}</button></footer>
      </section> : <section className="terminal-review-placeholder"><SquareTerminal size={26} /><strong>Review before launch</strong><p>Core will revalidate the assigned runtime, image digest, policy revision, DNS pins, workspace, and fixed limits before issuing a one-use connection ticket.</p></section>}
    </div>
  </div>;
}
