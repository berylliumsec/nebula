import { useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import {
  AlertTriangle,
  CircleStop,
  LoaderCircle,
  RotateCcw,
  ShieldCheck,
  SquareTerminal,
} from "lucide-react";
import type { ApiClient } from "../api/client";
import { ContainerTerminalSocket, type ContainerTerminalSocketState } from "../api/containerTerminal";
import type {
  ContainerTerminalRequest,
  ContainerTerminalRuntimeSnapshot,
  ContainerTerminalSession,
  EvidenceSummary,
  EvidenceUploadRequest,
  SetupImagePreparation,
  TerminalCommandHistoryStatus,
} from "../api/types";
import {
  bindXtermSelectionActions,
  copySelectionText,
  useOptionalSelectionActions,
} from "./selection";
import { TerminalScreenshotAction } from "./TerminalScreenshotAction";

interface ContainerTerminalPanelProps {
  active?: boolean;
  api: ApiClient;
  engagementId: string;
  engagementName: string;
  capturedBy?: string;
  onUploadEvidence?: (request: EvidenceUploadRequest) => Promise<EvidenceSummary>;
  setupTerminalDetail?: string;
  setupTerminalStatus?: "detecting_runner" | "needs_runner" | "preparing_image" | "ready" | "disabled" | "error";
}

function idempotencyKey(): string {
  return `container-terminal-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
}

function waitForSetupPoll(signal: AbortSignal, milliseconds = 750): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    const onAbort = () => {
      globalThis.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function LiveContainerTerminal({
  api,
  active,
  capturedBy,
  engagementId,
  onUploadEvidence,
  session,
  runtime,
  onAnother,
}: {
  api: ApiClient;
  active: boolean;
  capturedBy?: string;
  engagementId: string;
  onUploadEvidence?: (request: EvidenceUploadRequest) => Promise<EvidenceSummary>;
  session: ContainerTerminalSession;
  runtime: ContainerTerminalRuntimeSnapshot;
  onAnother: () => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const selectionActions = useOptionalSelectionActions();
  const apiBaseUrl = api.baseUrl;
  const apiToken = api.getToken();
  const terminalRef = useRef<Terminal | undefined>(undefined);
  const activeRef = useRef(active);
  const socketRef = useRef<ContainerTerminalSocket | undefined>(undefined);
  const [state, setState] = useState<ContainerTerminalSocketState>("connecting");
  const [error, setError] = useState<string>();
  const [exit, setExit] = useState<{ outcome: string; exitCode?: number }>();
  const [auditHealth, setAuditHealth] = useState<TerminalCommandHistoryStatus>();
  const [auditHealthUnavailable, setAuditHealthUnavailable] = useState(false);

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
    terminalRef.current = terminal;
    host.querySelector("textarea")?.setAttribute("aria-label", "Terminal input");
    terminal.attachCustomKeyEventHandler((event) => {
      const copyShortcut = event.type === "keydown"
        && (event.ctrlKey || event.metaKey)
        && !event.altKey
        && event.key.toLowerCase() === "c";
      if (!copyShortcut || !terminal.hasSelection()) return true;
      event.preventDefault();
      event.stopPropagation();
      void copySelectionText(terminal.getSelection()).catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : "The selected terminal text could not be copied.");
      });
      return false;
    });
    const selectionBinding = selectionActions
      ? bindXtermSelectionActions(terminal, selectionActions.presentSelection, {
          source: {
            kind: "terminal",
            id: session.sessionId,
            label: "Terminal selection",
          },
          onClear: selectionActions.dismissSelection,
        })
      : undefined;

    const socket = new ContainerTerminalSocket({
      apiBaseUrl,
      token: apiToken,
      session,
      onState: setState,
      onOutput: (data) => terminal.write(data),
      onReady: () => {
        setError(undefined);
        if (activeRef.current) terminal.focus();
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
    // The ticket is one-use. Deferring the connection lets React StrictMode
    // discard its development-only effect pass without consuming that ticket.
    const connectTimer = globalThis.setTimeout(() => socket.connect(), 0);
    return () => {
      globalThis.clearTimeout(connectTimer);
      if (frame !== undefined) globalThis.cancelAnimationFrame?.(frame);
      observer?.disconnect();
      globalThis.removeEventListener("resize", fitTerminal);
      input.dispose();
      resize.dispose();
      selectionBinding?.dispose();
      socket.dispose();
      socketRef.current = undefined;
      terminalRef.current = undefined;
      terminal.dispose();
    };
  }, [apiBaseUrl, apiToken, selectionActions, session]);

  useEffect(() => {
    activeRef.current = active;
    if (active && state === "ready") terminalRef.current?.focus();
  }, [active, state]);

  useEffect(() => {
    if (typeof api.terminalCommandHistoryStatus !== "function") return;
    const controller = new AbortController();
    const refresh = async () => {
      try {
        setAuditHealth(await api.terminalCommandHistoryStatus(engagementId, controller.signal));
        setAuditHealthUnavailable(false);
      } catch {
        if (!controller.signal.aborted) setAuditHealthUnavailable(true);
      }
    };
    void refresh();
    const interval = globalThis.setInterval(() => void refresh(), 3_000);
    return () => {
      controller.abort();
      globalThis.clearInterval(interval);
    };
  }, [api, engagementId]);

  const auditWarningCount = (auditHealth?.degradedCount ?? 0)
    + (auditHealth?.truncatedCount ?? 0)
    + (auditHealth?.auditGapCount ?? 0);

  const statusLabel = exit
    ? exit.outcome.replaceAll("_", " ")
    : state === "ready"
      ? "Connected"
      : state === "connecting"
        ? "Starting container…"
        : state.replaceAll("_", " ");

  return <div className="container-terminal-live">
    <header>
      <div><span className={`status-dot ${state === "ready" ? "healthy" : state === "error" ? "unavailable" : "warning"}`} /><span><strong>{statusLabel}</strong><small>Unrestricted outbound · root · writable · Kali headless <code title={`${runtime.image}\nOfficial base: ${runtime.baseImage}`}>{runtime.imageDigest.slice(0, 19)}…</code></small></span></div>
      <div className="terminal-header-actions">
        <TerminalScreenshotAction
          capturedBy={capturedBy}
          engagementId={engagementId}
          getTerminal={() => terminalRef.current}
          runtime={runtime}
          session={session}
          uploadEvidence={onUploadEvidence ?? ((request) => api.uploadEvidence(request))}
        />
        {exit ? <button className="button secondary" type="button" onClick={onAnother}><RotateCcw size={15} /> New terminal</button> : <button className="button danger" type="button" disabled={state === "closing" || state === "closed"} onClick={() => socketRef.current?.requestClose()}><CircleStop size={15} /> Stop terminal</button>}
      </div>
    </header>
    <div className="terminal-live-notices">
      {error && <p className="terminal-error" role="alert">{error}</p>}
      {(auditWarningCount > 0 || auditHealthUnavailable) && <p className="terminal-audit-warning" role="alert"><AlertTriangle size={14} /> {auditHealthUnavailable ? "Terminal audit health is unavailable. Capture failures cannot be ruled out." : `${auditWarningCount} terminal audit warning${auditWarningCount === 1 ? "" : "s"} detected. Review Terminal Audit for truncation, interruption, recovery, or persistence gaps.`}</p>}
      <p className="terminal-audit-active"><ShieldCheck size={14} /> Audit capture active · commands and merged PTY results are retained for this Project.</p>
      <p>Installed baseline: <code>kali-linux-headless</code> and <code>iputils-ping</code>. The official base is <code title={runtime.baseImage}>{runtime.baseImageDigest.slice(0, 19)}…</code>.</p>
      <p className="terminal-network-warning"><AlertTriangle size={14} /> Bridge networking can reach the public Internet and any host-addressable service. No ports, raw-packet capabilities, host shell, or runtime socket are granted.</p>
    </div>
    <div className="xterm-shell" ref={hostRef} aria-label="Terminal output" />
    <footer><ShieldCheck size={14} /> Additional system changes and packages disappear when this content-pinned container closes; the Kali headless baseline and <code>/workspace</code> remain available in new sessions.{exit?.exitCode !== undefined ? ` Exit code ${exit.exitCode}.` : ""}</footer>
  </div>;
}

export function ContainerTerminalPanel({
  active = true,
  api,
  capturedBy,
  engagementId,
  engagementName,
  onUploadEvidence,
  setupTerminalStatus,
}: ContainerTerminalPanelProps) {
  const instanceKey = useRef(idempotencyKey());
  const apiBaseUrl = api.baseUrl;
  const apiToken = api.getToken();
  const [launchAttempt, setLaunchAttempt] = useState(0);
  const [phase, setPhase] = useState<"detecting" | "checking" | "preparing" | "starting">(
    setupTerminalStatus === "detecting_runner"
      ? "detecting"
      : setupTerminalStatus === "preparing_image" ? "preparing" : "checking",
  );
  const [phaseDetail, setPhaseDetail] = useState<string>();
  const [imagePreparation, setImagePreparation] = useState<SetupImagePreparation>();
  const [session, setSession] = useState<{
    engagementId: string;
    value: ContainerTerminalSession;
    runtime: ContainerTerminalRuntimeSnapshot;
  }>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    const controller = new AbortController();
    setSession(undefined);
    setError(undefined);
    setImagePreparation(undefined);
    const request: ContainerTerminalRequest = {
      engagementId,
      columns: 100,
      rows: 30,
    };
    const launch = async () => {
      try {
        setPhase("checking");
        setPhaseDetail("Restoring any active Project terminal…");
        const recovered = await api.recoverContainerTerminal(
          engagementId,
          controller.signal,
        );
        if (recovered.active) {
          if (!recovered.session || !recovered.runtime) {
            throw new Error("Core returned incomplete active terminal recovery data.");
          }
          if (!controller.signal.aborted) {
            setSession({
              engagementId,
              value: recovered.session,
              runtime: recovered.runtime,
            });
          }
          return;
        }

        let setup = await api.setupStatus(controller.signal);
        let terminalStatus = setup.terminal.status;
        let terminalDetail = setup.terminal.detail;
        let readinessChecks = 0;
        while (terminalStatus === "detecting_runner") {
          setPhase("detecting");
          setPhaseDetail(terminalDetail);
          await waitForSetupPoll(controller.signal);
          setup = await api.setupStatus(controller.signal);
          terminalStatus = setup.terminal.status;
          terminalDetail = setup.terminal.detail;
        }
        if (terminalStatus === "needs_runner" || terminalStatus === "disabled"
          || (terminalStatus === "error" && setup.terminal.imagePreparation.phase !== "error")) {
          throw new Error(terminalDetail ?? "A verified local container runner is required to use Terminal.");
        }

        let preparation = setup.terminal.imagePreparation;
        setImagePreparation(preparation);
        if (preparation.phase === "not_started") {
          const control = await api.prepareSetupImage(engagementId, controller.signal);
          setup = control.setup;
          preparation = setup.terminal.imagePreparation;
          setImagePreparation(preparation);
        } else if (preparation.phase === "error" || preparation.phase === "cancelled") {
          const control = await api.retrySetupImage(engagementId, controller.signal);
          setup = control.setup;
          preparation = setup.terminal.imagePreparation;
          setImagePreparation(preparation);
        }
        while (["queued", "resolving_runtime", "preparing_image", "cancelling"].includes(preparation.phase)) {
          setPhase("preparing");
          setPhaseDetail(preparation.detail ?? setup.terminal.detail);
          await waitForSetupPoll(controller.signal, 500);
          setup = await api.setupStatus(controller.signal);
          preparation = setup.terminal.imagePreparation;
          setImagePreparation(preparation);
        }
        if (preparation.phase === "error" || preparation.phase === "cancelled") {
          throw new Error(preparation.detail ?? "Workstation image preparation did not complete.");
        }
        if (preparation.phase !== "ready") {
          throw new Error("Workstation image preparation did not reach a ready state.");
        }

        setPhase("checking");
        setPhaseDetail(undefined);
        let capabilities = await api.containerTerminalCapabilities(engagementId, controller.signal);
        while (!capabilities.ready && readinessChecks < 12) {
          const setup = await api.setupStatus(controller.signal);
          terminalStatus = setup.terminal.status;
          terminalDetail = setup.terminal.detail;
          if (terminalStatus === "needs_runner" || terminalStatus === "disabled" || terminalStatus === "error") {
            throw new Error(terminalDetail ?? capabilities.detail ?? "A verified local container runner is required to use Terminal.");
          }
          setPhase(terminalStatus === "preparing_image" ? "preparing" : terminalStatus === "detecting_runner" ? "detecting" : "checking");
          setPhaseDetail(terminalDetail ?? capabilities.detail);
          await waitForSetupPoll(controller.signal);
          capabilities = await api.containerTerminalCapabilities(engagementId, controller.signal);
          readinessChecks += 1;
        }
        if (!capabilities.ready) {
          throw new Error(capabilities.detail ?? terminalDetail ?? "A verified local container runner is required to use Terminal.");
        }

        setPhase("preparing");
        setPhaseDetail(undefined);
        const preview = await api.preflightContainerTerminal(request, controller.signal);
        if (!preview.allowed || !preview.previewToken || !preview.previewFingerprint || !preview.runtime) {
          throw new Error(preview.detail || "Core denied the terminal preflight.");
        }

        setPhase("starting");
        setPhaseDetail(undefined);
        let created: ContainerTerminalSession;
        try {
          created = await api.startContainerTerminal(
            request,
            preview,
            `${instanceKey.current}-${engagementId}-${launchAttempt}`,
            controller.signal,
          );
        } catch (startError) {
          // A second view can win the start race between our initial recovery
          // check and this request. Recover that one active Project terminal
          // instead of presenting a duplicate-start dead end.
          try {
            const raced = await api.recoverContainerTerminal(
              engagementId,
              controller.signal,
            );
            if (raced.active && raced.session && raced.runtime) {
              if (!controller.signal.aborted) {
                setSession({
                  engagementId,
                  value: raced.session,
                  runtime: raced.runtime,
                });
              }
              return;
            }
          } catch {
            // Preserve the actionable start failure when no active terminal
            // can be recovered.
          }
          throw startError;
        }
        if (!controller.signal.aborted) setSession({ engagementId, value: created, runtime: preview.runtime });
      } catch (reason) {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "Could not start Terminal.");
        }
      }
    };
    // Deferring the first network request prevents React StrictMode's
    // development-only effect probe from creating a duplicate terminal.
    const launchTimer = globalThis.setTimeout(() => void launch(), 0);
    return () => {
      globalThis.clearTimeout(launchTimer);
      controller.abort();
    };
  // ApiClient is reconstructed while initial project selection settles. The
  // same endpoint/token is the same Core connection and must not abort launch.
  }, [apiBaseUrl, apiToken, engagementId, launchAttempt, setupTerminalStatus]);

  const cancelImagePreparation = async () => {
    if (!imagePreparation?.operationId || !imagePreparation.canCancel) return;
    setPhaseDetail("Cancelling workstation image preparation…");
    try {
      const control = await api.cancelSetupImage(imagePreparation.operationId);
      setImagePreparation(control.setup.terminal.imagePreparation);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not cancel image preparation.");
    }
  };

  if (session?.engagementId === engagementId) {
    return <LiveContainerTerminal active={active} api={api} capturedBy={capturedBy} engagementId={engagementId} onUploadEvidence={onUploadEvidence} session={session.value} runtime={session.runtime} onAnother={() => {
      setLaunchAttempt((value) => value + 1);
    }} />;
  }

  const status = phase === "detecting"
    ? "Detecting a supported local container runtime…"
    : phase === "checking"
      ? "Checking the verified container runner…"
    : phase === "preparing"
      ? "Preparing the Kali headless tool image…"
      : "Starting the content-pinned Kali terminal…";

  return <div className="container-terminal-panel">
    <section className="container-terminal-intro">
      <span className="terminal-hero-icon"><SquareTerminal size={23} /></span>
      <div><small>Kali shell</small><h2>Terminal</h2><p>A fresh Kali Rolling container starts for <strong>{engagementName}</strong> as root with a writable disposable filesystem and unrestricted outbound networking. Nebula derives it from the verified official image with the <code>kali-linux-headless</code> toolset and <code>iputils-ping</code> preinstalled. Additional packages installed with <code>apt</code> disappear when the session closes, while <code>/workspace</code> persists.</p></div>
      <span className="terminal-boundary"><AlertTriangle size={15} /> Root + network</span>
    </section>
    <section className="terminal-auto-start" aria-live="polite">
      {error ? <><SquareTerminal size={27} /><strong>Terminal could not start</strong><p className="terminal-error" role="alert">{error}</p><button className="button primary" type="button" onClick={() => setLaunchAttempt((value) => value + 1)}><RotateCcw size={15} /> Retry</button></> : <><LoaderCircle className="spin" size={27} /><strong>{status}</strong><p>{phaseDetail ?? <>Terminal verifies the configured official image, prepares the cached <code>kali-linux-headless</code> workstation, and launches its immutable image ID with no host shell or runtime socket. The first preparation can take several minutes.</>}</p>{imagePreparation?.progressPercent !== undefined && <progress max={100} value={imagePreparation.progressPercent} aria-label="Workstation image preparation progress" />}{phase === "preparing" && <small>Image layers use local container-runtime storage. Cached verified launches do not contact the registry.</small>}{imagePreparation?.canCancel && <button className="button secondary" type="button" onClick={() => void cancelImagePreparation()}><CircleStop size={15} /> Cancel preparation</button>}</>}
    </section>
  </div>;
}
