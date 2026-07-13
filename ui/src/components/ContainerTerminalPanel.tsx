import { useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import {
  CircleStop,
  LoaderCircle,
  RotateCcw,
  ShieldCheck,
  SquareTerminal,
} from "lucide-react";
import type { ApiClient } from "../api/client";
import { ContainerTerminalSocket, type ContainerTerminalSocketState } from "../api/containerTerminal";
import type { ContainerTerminalRequest, ContainerTerminalSession } from "../api/types";

interface ContainerTerminalPanelProps {
  api: ApiClient;
  engagementId: string;
  engagementName: string;
}

function idempotencyKey(): string {
  return `container-terminal-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
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
    host.querySelector("textarea")?.setAttribute("aria-label", "Terminal input");

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
    <div className="xterm-shell" ref={hostRef} aria-label="Terminal output" />
    <footer><ShieldCheck size={14} /> This terminal is inside the pinned container. Leaving this view closes and removes it.{exit?.exitCode !== undefined ? ` Exit code ${exit.exitCode}.` : ""}</footer>
  </div>;
}

export function ContainerTerminalPanel({ api, engagementId, engagementName }: ContainerTerminalPanelProps) {
  const instanceKey = useRef(idempotencyKey());
  const [launchAttempt, setLaunchAttempt] = useState(0);
  const [phase, setPhase] = useState<"checking" | "reviewing" | "starting">("checking");
  const [session, setSession] = useState<{ engagementId: string; value: ContainerTerminalSession }>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    const controller = new AbortController();
    setSession(undefined);
    setError(undefined);
    const request: ContainerTerminalRequest = {
      engagementId,
      network: { mode: "none", ports: [] },
      columns: 100,
      rows: 30,
    };
    const launch = async () => {
      try {
        setPhase("checking");
        const capabilities = await api.containerTerminalCapabilities(engagementId, controller.signal);
        if (!capabilities.ready || !capabilities.offline) {
          throw new Error(capabilities.detail ?? "A ready offline Toolbox runtime is required to use Terminal.");
        }

        setPhase("reviewing");
        const preview = await api.preflightContainerTerminal(request, controller.signal);
        if (!preview.allowed || !preview.previewToken || !preview.previewFingerprint || !preview.runtime || !preview.network) {
          throw new Error(preview.detail || "Core denied the terminal preflight.");
        }

        setPhase("starting");
        const created = await api.startContainerTerminal(
          request,
          preview,
          `${instanceKey.current}-${engagementId}-${launchAttempt}`,
          controller.signal,
        );
        if (!controller.signal.aborted) setSession({ engagementId, value: created });
      } catch (reason) {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "Could not start Terminal.");
        }
      }
    };
    void launch();
    return () => controller.abort();
  }, [api, engagementId, launchAttempt]);

  if (session?.engagementId === engagementId) {
    return <LiveContainerTerminal api={api} session={session.value} networkLabel="Offline" onAnother={() => {
      setLaunchAttempt((value) => value + 1);
    }} />;
  }

  const status = phase === "checking"
    ? "Checking the assigned Toolbox runtime…"
    : phase === "reviewing"
      ? "Validating the isolated terminal boundary…"
      : "Starting Terminal…";

  return <div className="container-terminal-panel">
    <section className="container-terminal-intro">
      <span className="terminal-hero-icon"><SquareTerminal size={23} /></span>
      <div><small>Automatic isolated shell</small><h2>Terminal</h2><p>An interactive bash session starts automatically in a fresh, pinned Toolbox container for <strong>{engagementName}</strong>. The only persistent mount is this engagement’s <code>/workspace</code>.</p></div>
      <span className="terminal-boundary"><ShieldCheck size={15} /> No host shell</span>
    </section>
    <section className="terminal-auto-start" aria-live="polite">
      {error ? <><SquareTerminal size={27} /><strong>Terminal could not start</strong><p className="terminal-error" role="alert">{error}</p><button className="button primary" type="button" onClick={() => setLaunchAttempt((value) => value + 1)}><RotateCcw size={15} /> Retry</button></> : <><LoaderCircle className="spin" size={27} /><strong>{status}</strong><p>Terminal uses an offline network namespace and opens as soon as Core completes its automatic preflight.</p></>}
    </section>
  </div>;
}
