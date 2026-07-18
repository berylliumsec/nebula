import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import {
  AlertTriangle,
  ChevronDown,
  CircleStop,
  LoaderCircle,
  Plus,
  RotateCcw,
  ShieldCheck,
  SquareTerminal,
  X,
} from "lucide-react";
import type { ApiClient } from "../api/client";
import { ContainerTerminalSocket, type ContainerTerminalExit, type ContainerTerminalSocketState } from "../api/containerTerminal";
import type {
  ContainerTerminalCapacity,
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
import { useConfirmation } from "./DialogSystem";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

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

type LaunchPhase = "detecting" | "checking" | "preparing" | "starting";

interface StartingTerminalTab {
  kind: "starting";
  key: string;
  clientIdempotencyKey: string;
  ordinal: number;
  phase: LaunchPhase;
  phaseDetail?: string;
  imagePreparation?: SetupImagePreparation;
  error?: string;
}

interface LiveTerminalTab {
  kind: "live";
  key: string;
  ordinal: number;
  session: ContainerTerminalSession;
  runtime: ContainerTerminalRuntimeSnapshot;
  socketState: ContainerTerminalSocketState;
  exit?: ContainerTerminalExit;
  error?: string;
}

type TerminalTab = StartingTerminalTab | LiveTerminalTab;

function idempotencyKey(): string {
  return `container-terminal-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
}

function terminalTabKey(): string {
  return `terminal-tab-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
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
  auditHealth,
  auditHealthUnavailable,
  capturedBy,
  engagementId,
  managementError,
  onExit,
  onNewTerminal,
  onSocketState,
  onUploadEvidence,
  session,
  runtime,
}: {
  api: ApiClient;
  active: boolean;
  auditHealth?: TerminalCommandHistoryStatus;
  auditHealthUnavailable: boolean;
  capturedBy?: string;
  engagementId: string;
  managementError?: string;
  onExit: (result: ContainerTerminalExit) => void;
  onNewTerminal: () => void;
  onSocketState: (state: ContainerTerminalSocketState) => void;
  onUploadEvidence?: (request: EvidenceUploadRequest) => Promise<EvidenceSummary>;
  session: ContainerTerminalSession;
  runtime: ContainerTerminalRuntimeSnapshot;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const selectionActions = useOptionalSelectionActions();
  const apiBaseUrl = api.baseUrl;
  const apiToken = api.getToken();
  const terminalRef = useRef<Terminal | undefined>(undefined);
  const fitRef = useRef<FitAddon | undefined>(undefined);
  const activeRef = useRef(active);
  const socketRef = useRef<ContainerTerminalSocket | undefined>(undefined);
  const [state, setState] = useState<ContainerTerminalSocketState>("connecting");
  const [error, setError] = useState<string>();
  const [exit, setExit] = useState<ContainerTerminalExit>();

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const terminal = new Terminal({
      // A steady cursor remains identifiable in dark themes and when the
      // terminal renderer does not receive reliable focus/blur repaint events.
      cursorBlink: false,
      cursorStyle: "block",
      cursorInactiveStyle: "block",
      fontFamily: '"Noto Sans Mono", "SFMono-Regular", Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.25,
      scrollback: 10_000,
      theme: {
        background: "#071017",
        foreground: "#d9e5e9",
        cursor: "#b8ffe3",
        cursorAccent: "#071017",
        // Keep selection unmistakable against the terminal canvas. The inactive
        // color remains high-contrast because the selection-actions toolbar can
        // temporarily move focus away from xterm without clearing the selection.
        selectionBackground: "#168bd2",
        selectionInactiveBackground: "#126fa8",
        selectionForeground: "#ffffff",
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
    fitRef.current = fit;
    host.querySelector("textarea")?.setAttribute("aria-label", "Terminal input");
    const focusTerminal = () => {
      if (activeRef.current) terminal.focus();
    };
    host.addEventListener("pointerdown", focusTerminal);
    globalThis.addEventListener("focus", focusTerminal);
    terminal.attachCustomKeyEventHandler((event) => {
      const copyShortcut = event.type === "keydown"
        && (event.ctrlKey || event.metaKey)
        && !event.altKey
        && event.key.toLowerCase() === "c";
      if (!copyShortcut || !terminal.hasSelection()) return true;
      event.preventDefault();
      event.stopPropagation();
      void copySelectionText(terminal.getSelection()).catch((reason: unknown) => {
        void logCaughtDiagnostic("interface.container_terminal_panel.caught_failure_01", "A handled interface operation failed.", reason, "container_terminal_panel");
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

    const updateState = (next: ContainerTerminalSocketState) => {
      setState(next);
      onSocketState(next);
    };
    const socket = new ContainerTerminalSocket({
      apiBaseUrl,
      token: apiToken,
      session,
      onState: updateState,
      onOutput: (data) => terminal.write(data),
      onReady: () => {
        setError(undefined);
        if (activeRef.current) terminal.focus();
      },
      onError: (_code, detail) => setError(detail),
      onExit: (result) => {
        setExit(result);
        if (result.detail) setError(result.detail);
        onExit(result);
      },
    });
    socketRef.current = socket;
    const input = terminal.onData((data) => socket.sendInput(data));
    const resize = terminal.onResize(({ cols, rows }) => socket.resize(cols, rows));
    const fitTerminal = () => {
      try {
        fit.fit();
        if (terminal.cols > 0 && terminal.rows > 0) socket.resize(terminal.cols, terminal.rows);
      } catch (caughtError) {
        void logCaughtDiagnostic("interface.container_terminal_panel.caught_failure_02", "A handled interface operation failed.", caughtError, "container_terminal_panel");
        // Hidden tab panels briefly have zero dimensions.
      }
    };
    const frame = globalThis.requestAnimationFrame?.(fitTerminal);
    const observer = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(fitTerminal);
    observer?.observe(host);
    globalThis.addEventListener("resize", fitTerminal);
    // Defer the one-use ticket so React StrictMode can discard its probe pass.
    const connectTimer = globalThis.setTimeout(() => socket.connect(), 0);
    return () => {
      globalThis.clearTimeout(connectTimer);
      if (frame !== undefined) globalThis.cancelAnimationFrame?.(frame);
      observer?.disconnect();
      host.removeEventListener("pointerdown", focusTerminal);
      globalThis.removeEventListener("focus", focusTerminal);
      globalThis.removeEventListener("resize", fitTerminal);
      input.dispose();
      resize.dispose();
      selectionBinding?.dispose();
      socket.dispose();
      socketRef.current = undefined;
      terminalRef.current = undefined;
      fitRef.current = undefined;
      terminal.dispose();
    };
  }, [apiBaseUrl, apiToken, selectionActions, session]);

  useEffect(() => {
    activeRef.current = active;
    if (!active) return;
    const frame = globalThis.requestAnimationFrame?.(() => {
      try {
        fitRef.current?.fit();
        const terminal = terminalRef.current;
        if (terminal && terminal.cols > 0 && terminal.rows > 0) {
          socketRef.current?.resize(terminal.cols, terminal.rows);
        }
        if (state === "ready") terminal?.focus();
      } catch (caughtError) {
        void logCaughtDiagnostic("interface.container_terminal_panel.caught_failure_03", "A handled interface operation failed.", caughtError, "container_terminal_panel");
      }
    });
    return () => {
      if (frame !== undefined) globalThis.cancelAnimationFrame?.(frame);
    };
  }, [active, state]);

  const auditWarningCount = (auditHealth?.degradedCount ?? 0)
    + (auditHealth?.truncatedCount ?? 0)
    + (auditHealth?.auditGapCount ?? 0)
    + (auditHealth?.classificationFailureCount ?? 0);

  const failedExit = exit?.outcome === "failed" || exit?.outcome === "disconnected";
  const statusLabel = exit
    ? exit.outcome.replaceAll("_", " ")
    : state === "ready"
      ? "Connected"
      : state === "connecting"
        ? "Starting container…"
        : state.replaceAll("_", " ");

  return <div className="container-terminal-live">
    <header>
      <div><span className={`status-dot ${failedExit || state === "error" ? "unavailable" : state === "ready" ? "healthy" : "warning"}`} /><span><strong>{statusLabel}</strong><small>Unrestricted outbound · root · writable · Kali headless <code title={`${runtime.image}\nOfficial base: ${runtime.baseImage}`}>{runtime.imageDigest.slice(0, 19)}…</code></small></span></div>
      <div className="terminal-header-actions">
        <TerminalScreenshotAction
          capturedBy={capturedBy}
          engagementId={engagementId}
          getTerminal={() => terminalRef.current}
          runtime={runtime}
          session={session}
          uploadEvidence={onUploadEvidence ?? ((request) => api.uploadEvidence(request))}
        />
        {exit ? <button className="button secondary" type="button" onClick={onNewTerminal}><Plus size={15} /> New terminal</button> : <button className="button danger" type="button" disabled={state === "closing" || state === "closed"} onClick={() => socketRef.current?.requestClose()}><CircleStop size={15} /> Stop terminal</button>}
      </div>
    </header>
    <div className="terminal-live-notices">
      {error && <DiagnosticErrorNotice error={error} fallback="The terminal operation could not be completed." compact />}
      {managementError && <DiagnosticErrorNotice error={managementError} fallback="The terminal could not be stopped." compact />}
      {(auditWarningCount > 0 || auditHealthUnavailable) && <p className="terminal-audit-warning" role="alert"><AlertTriangle size={14} /> {auditHealthUnavailable ? "Terminal audit health is unavailable. Capture failures cannot be ruled out." : `${auditWarningCount} terminal audit warning${auditWarningCount === 1 ? "" : "s"} detected. Review Terminal Audit for classification, truncation, interruption, recovery, or persistence gaps.`}</p>}
      <p className="terminal-audit-active"><ShieldCheck size={14} /> Selective audit active</p>
      <p><code>kali-linux-headless</code> · <code title={runtime.baseImage}>{runtime.baseImageDigest.slice(0, 19)}…</code></p>
      <p className="terminal-network-warning"><AlertTriangle size={14} /> Bridge networking can reach the public Internet and any host-addressable service. No ports, raw-packet capabilities, host shell, or runtime socket are granted.</p>
    </div>
    <div className="xterm-shell" ref={hostRef} aria-label="Terminal output" />
    <footer><ShieldCheck size={14} /> Additional system changes and packages disappear when this content-pinned container closes; the Kali headless baseline and <code>/workspace</code> remain available in new sessions.{exit?.exitCode !== undefined ? ` Exit code ${exit.exitCode}.` : ""}</footer>
  </div>;
}

function StartingTerminalPanel({
  engagementName,
  tab,
  onCancelPreparation,
  onClose,
  onRetry,
}: {
  engagementName: string;
  tab: StartingTerminalTab;
  onCancelPreparation: () => void;
  onClose: () => void;
  onRetry: () => void;
}) {
  const preparationPhase = tab.imagePreparation?.phase;
  const status = tab.phase === "detecting"
    ? "Finding container runtime"
    : tab.phase === "checking"
      ? "Checking Kali runtime"
      : tab.phase === "preparing"
        ? preparationPhase === "queued"
          ? "Kali preparation queued"
          : preparationPhase === "resolving_runtime"
            ? "Checking container runtime"
            : preparationPhase === "cancelling"
              ? "Stopping preparation"
              : "Preparing Kali runtime"
        : "Starting Kali terminal";
  const progressPercent = tab.imagePreparation?.progressPercent;
  const progressIndeterminate = progressPercent === undefined
    || tab.imagePreparation?.progressIndeterminate === true;
  const progressDetail = tab.phaseDetail
    ?? (tab.phase === "detecting"
      ? "Looking for Docker or Podman on this Mac."
      : tab.phase === "checking"
        ? "Verifying the local container runtime and cached image."
        : tab.phase === "preparing"
          ? "Pulling or reusing Kali, then verifying the prepared image."
          : "Creating the terminal session and connecting its console.");

  return <div className="container-terminal-panel">
    <section className="container-terminal-intro">
      <span className="terminal-hero-icon"><SquareTerminal size={23} /></span>
      <div><small>Kali shell · {engagementName}</small><h2>Terminal {tab.ordinal}</h2><p><code>kali-linux-headless</code> · disposable · shared <code>/workspace</code></p></div>
      <span className="terminal-boundary"><AlertTriangle size={15} /> Root + network</span>
    </section>
    <section className="terminal-auto-start" aria-live="polite">
      {tab.error ? <><SquareTerminal size={27} /><strong>Terminal could not start</strong><DiagnosticErrorNotice error={tab.error} fallback="The terminal operation could not be completed." compact /><div className="terminal-start-actions"><button className="button secondary" type="button" onClick={onClose}>Close</button><button className="button primary" type="button" onClick={onRetry}><RotateCcw size={15} /> Retry</button></div></> : <><LoaderCircle className="spin" size={27} /><strong>{status}</strong><div
        className={`terminal-start-progress${progressIndeterminate ? " indeterminate" : ""}`}
        role="progressbar"
        aria-label="Kali terminal startup progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progressIndeterminate ? undefined : progressPercent}
        aria-valuetext={progressIndeterminate ? status : `${progressPercent}%`}
      ><span style={progressIndeterminate ? undefined : { width: `${progressPercent}%` }} /></div><small className="terminal-start-detail">{progressDetail}</small>{tab.imagePreparation?.canCancel && <button className="button secondary" type="button" onClick={onCancelPreparation}><CircleStop size={15} /> Cancel</button>}</>}
    </section>
  </div>;
}

function tabStatus(tab: TerminalTab): "healthy" | "warning" | "unavailable" | "muted" {
  if (tab.kind === "starting") return tab.error ? "unavailable" : "warning";
  if (tab.exit) return ["failed", "disconnected"].includes(tab.exit.outcome) ? "unavailable" : "muted";
  if (tab.socketState === "ready") return "healthy";
  if (tab.socketState === "error" || tab.error) return "unavailable";
  return "warning";
}

export function ContainerTerminalPanel({
  active = true,
  api,
  capturedBy,
  engagementId,
  engagementName,
  onUploadEvidence,
  setupTerminalDetail,
  setupTerminalStatus,
}: ContainerTerminalPanelProps) {
  const confirm = useConfirmation();
  const apiBaseUrl = api.baseUrl;
  const apiToken = api.getToken();
  const projectAbortRef = useRef<AbortController | undefined>(undefined);
  const launchControllersRef = useRef(new Map<string, AbortController>());
  const launchingKeyRef = useRef<string | undefined>(undefined);
  const setupReadyRef = useRef(false);
  const nextOrdinalRef = useRef(1);
  const [bootstrapAttempt, setBootstrapAttempt] = useState(0);
  const [tabs, setTabs] = useState<TerminalTab[]>([]);
  const [activeKey, setActiveKey] = useState<string>();
  const [capacity, setCapacity] = useState<ContainerTerminalCapacity>({
    activeSessions: 0,
    availableSessions: 32,
    maxActiveSessions: 32,
  });
  const [initializing, setInitializing] = useState(true);
  const [initialError, setInitialError] = useState<string>();
  const [launchingKey, setLaunchingKey] = useState<string>();
  const [overflowOpen, setOverflowOpen] = useState(false);
  const [auditHealth, setAuditHealth] = useState<TerminalCommandHistoryStatus>();
  const [auditHealthUnavailable, setAuditHealthUnavailable] = useState(false);

  const updateStartingTab = (key: string, update: Partial<StartingTerminalTab>) => {
    setTabs((current) => current.map((tab) => tab.key === key && tab.kind === "starting"
      ? { ...tab, ...update, kind: "starting" }
      : tab));
  };

  const refreshCapacity = async (signal?: AbortSignal) => {
    try {
      const current = await api.containerTerminalCapacity(signal);
      if (!signal?.aborted) setCapacity(current);
    } catch (reason) {
      void logCaughtDiagnostic("interface.container_terminal_panel.caught_failure_04", "A handled interface operation failed.", reason, "container_terminal_panel");
    }
  };

  const ensureSetup = async (key: string, signal: AbortSignal) => {
    if (setupReadyRef.current) return;
    let setup = await api.setupStatus(signal);
    let terminalStatus = setup.terminal.status;
    let terminalDetail = setup.terminal.detail;
    while (terminalStatus === "detecting_runner") {
      updateStartingTab(key, { phase: "detecting", phaseDetail: terminalDetail });
      await waitForSetupPoll(signal);
      setup = await api.setupStatus(signal);
      terminalStatus = setup.terminal.status;
      terminalDetail = setup.terminal.detail;
    }
    if (terminalStatus === "needs_runner" || terminalStatus === "disabled"
      || (terminalStatus === "error" && setup.terminal.imagePreparation.phase !== "error")) {
      throw new Error(terminalDetail ?? "A verified local container runner is required to use Terminal.");
    }

    let preparation = setup.terminal.imagePreparation;
    updateStartingTab(key, { imagePreparation: preparation });
    if (preparation.phase === "not_started") {
      const control = await api.prepareSetupImage(engagementId, signal);
      setup = control.setup;
      preparation = setup.terminal.imagePreparation;
      updateStartingTab(key, { imagePreparation: preparation });
    } else if (preparation.phase === "error" || preparation.phase === "cancelled") {
      const control = await api.retrySetupImage(engagementId, signal);
      setup = control.setup;
      preparation = setup.terminal.imagePreparation;
      updateStartingTab(key, { imagePreparation: preparation });
    }
    while (["queued", "resolving_runtime", "preparing_image", "cancelling"].includes(preparation.phase)) {
      updateStartingTab(key, {
        phase: "preparing",
        phaseDetail: preparation.detail ?? setup.terminal.detail,
        imagePreparation: preparation,
      });
      await waitForSetupPoll(signal, 500);
      setup = await api.setupStatus(signal);
      preparation = setup.terminal.imagePreparation;
    }
    updateStartingTab(key, { imagePreparation: preparation });
    if (preparation.phase === "error" || preparation.phase === "cancelled") {
      throw new Error(preparation.detail ?? "Workstation image preparation did not complete.");
    }
    if (preparation.phase !== "ready") {
      throw new Error("Workstation image preparation did not reach a ready state.");
    }
    setupReadyRef.current = true;
  };

  const launchTab = async (key: string, ordinal: number, clientIdempotencyKey: string) => {
    if (launchingKeyRef.current && launchingKeyRef.current !== key) return;
    const projectController = projectAbortRef.current;
    if (!projectController || projectController.signal.aborted) return;
    launchingKeyRef.current = key;
    const controller = new AbortController();
    const abortLaunch = () => controller.abort();
    projectController.signal.addEventListener("abort", abortLaunch, { once: true });
    launchControllersRef.current.set(key, controller);
    setLaunchingKey(key);
    updateStartingTab(key, {
      phase: setupReadyRef.current ? "checking" : setupTerminalStatus === "detecting_runner" ? "detecting" : setupTerminalStatus === "preparing_image" ? "preparing" : "checking",
      phaseDetail: setupReadyRef.current ? undefined : setupTerminalDetail,
      error: undefined,
      imagePreparation: undefined,
    });
    try {
      await ensureSetup(key, controller.signal);
      let capabilities = await api.containerTerminalCapabilities(engagementId, controller.signal);
      let readinessChecks = 0;
      while (!capabilities.ready && readinessChecks < 12) {
        const setup = await api.setupStatus(controller.signal);
        const terminalStatus = setup.terminal.status;
        const terminalDetail = setup.terminal.detail;
        if (terminalStatus === "needs_runner" || terminalStatus === "disabled" || terminalStatus === "error") {
          throw new Error(terminalDetail ?? capabilities.detail ?? "A verified local container runner is required to use Terminal.");
        }
        updateStartingTab(key, {
          phase: terminalStatus === "preparing_image" ? "preparing" : terminalStatus === "detecting_runner" ? "detecting" : "checking",
          phaseDetail: terminalDetail ?? capabilities.detail,
        });
        await waitForSetupPoll(controller.signal);
        capabilities = await api.containerTerminalCapabilities(engagementId, controller.signal);
        readinessChecks += 1;
      }
      if (!capabilities.ready) {
        throw new Error(capabilities.detail ?? "A verified local container runner is required to use Terminal.");
      }

      const request: ContainerTerminalRequest = { engagementId, columns: 100, rows: 30 };
      updateStartingTab(key, { phase: "preparing", phaseDetail: undefined });
      const preview = await api.preflightContainerTerminal(request, controller.signal);
      if (!preview.allowed || !preview.previewToken || !preview.previewFingerprint || !preview.runtime) {
        throw new Error(preview.detail || "Core denied the terminal preflight.");
      }
      updateStartingTab(key, { phase: "starting" });
      const created = await api.startContainerTerminal(
        request,
        preview,
        clientIdempotencyKey,
        controller.signal,
      );
      if (!controller.signal.aborted) {
        setTabs((current) => current.map((tab) => tab.key === key ? {
          kind: "live",
          key,
          ordinal,
          session: created,
          runtime: preview.runtime!,
          socketState: "connecting",
        } : tab));
        setupReadyRef.current = true;
        await refreshCapacity(controller.signal);
      }
    } catch (reason) {
      void logCaughtDiagnostic("interface.container_terminal_panel.caught_failure_05", "A handled interface operation failed.", reason, "container_terminal_panel");
      if (!controller.signal.aborted) {
        updateStartingTab(key, {
          error: reason instanceof Error ? reason.message : "Could not start Terminal.",
        });
        await refreshCapacity(controller.signal);
      }
    } finally {
      projectController.signal.removeEventListener("abort", abortLaunch);
      launchControllersRef.current.delete(key);
      if (launchingKeyRef.current === key) launchingKeyRef.current = undefined;
      setLaunchingKey((current) => current === key ? undefined : current);
    }
  };

  const addTerminal = () => {
    if (launchingKeyRef.current || capacity.availableSessions <= 0) return;
    const ordinal = nextOrdinalRef.current;
    nextOrdinalRef.current += 1;
    const key = terminalTabKey();
    const tab: StartingTerminalTab = {
      kind: "starting",
      key,
      clientIdempotencyKey: `${idempotencyKey()}-${engagementId}`,
      ordinal,
      phase: setupReadyRef.current ? "checking" : "detecting",
      phaseDetail: setupReadyRef.current ? undefined : setupTerminalDetail,
    };
    setTabs((current) => [...current, tab]);
    setActiveKey(key);
    setOverflowOpen(false);
    void launchTab(key, ordinal, tab.clientIdempotencyKey);
  };

  useEffect(() => {
    const controller = new AbortController();
    projectAbortRef.current = controller;
    setupReadyRef.current = false;
    launchingKeyRef.current = undefined;
    nextOrdinalRef.current = 1;
    setTabs([]);
    setActiveKey(undefined);
    setInitialError(undefined);
    setLaunchingKey(undefined);
    setInitializing(true);
    setOverflowOpen(false);

    const bootstrap = async () => {
      try {
        const recovered = await api.recoverContainerTerminals(engagementId, controller.signal);
        if (controller.signal.aborted) return;
        const recoveredTabs: LiveTerminalTab[] = recovered.sessions.map((item, index) => ({
          kind: "live",
          key: item.session.sessionId,
          ordinal: index + 1,
          session: item.session,
          runtime: item.runtime,
          socketState: "connecting",
        }));
        nextOrdinalRef.current = recoveredTabs.length + 1;
        if (recoveredTabs.length) {
          setupReadyRef.current = true;
          setTabs(recoveredTabs);
          setActiveKey(recoveredTabs.at(-1)?.key);
          setInitializing(false);
          await refreshCapacity(controller.signal);
          return;
        }
        setInitializing(false);
        const key = terminalTabKey();
        const first: StartingTerminalTab = {
          kind: "starting",
          key,
          clientIdempotencyKey: `${idempotencyKey()}-${engagementId}`,
          ordinal: 1,
          phase: setupTerminalStatus === "detecting_runner" ? "detecting" : setupTerminalStatus === "preparing_image" ? "preparing" : "checking",
          phaseDetail: setupTerminalDetail,
        };
        nextOrdinalRef.current = 2;
        setTabs([first]);
        setActiveKey(key);
        await launchTab(key, 1, first.clientIdempotencyKey);
      } catch (reason) {
        void logCaughtDiagnostic("interface.container_terminal_panel.caught_failure_06", "A handled interface operation failed.", reason, "container_terminal_panel");
        if (!controller.signal.aborted) {
          setInitialError(reason instanceof Error ? reason.message : "Could not restore Project terminals.");
          setInitializing(false);
        }
      }
    };
    const timer = globalThis.setTimeout(() => void bootstrap(), 0);
    return () => {
      globalThis.clearTimeout(timer);
      controller.abort();
      for (const launch of launchControllersRef.current.values()) launch.abort();
      launchControllersRef.current.clear();
      launchingKeyRef.current = undefined;
    };
  // The endpoint and token identify one Core connection. Setup detail changes
  // must not tear down active terminal sockets.
  }, [apiBaseUrl, apiToken, engagementId, bootstrapAttempt]);

  useEffect(() => {
    if (typeof api.terminalCommandHistoryStatus !== "function") return;
    const controller = new AbortController();
    const refresh = async () => {
      try {
        setAuditHealth(await api.terminalCommandHistoryStatus(engagementId, controller.signal));
        setAuditHealthUnavailable(false);
      } catch (caughtError) {
        void logCaughtDiagnostic("interface.container_terminal_panel.caught_failure_07", "A handled interface operation failed.", caughtError, "container_terminal_panel");
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

  const removeTab = (key: string) => {
    launchControllersRef.current.get(key)?.abort();
    launchControllersRef.current.delete(key);
    setTabs((current) => {
      const index = current.findIndex((tab) => tab.key === key);
      const remaining = current.filter((tab) => tab.key !== key);
      if (activeKey === key) {
        setActiveKey(remaining[Math.min(index, remaining.length - 1)]?.key);
      }
      return remaining;
    });
  };

  const closeTab = async (tab: TerminalTab) => {
    if (tab.kind === "starting") {
      removeTab(tab.key);
      return;
    }
    if (!tab.exit) {
      const approved = await confirm({
        title: `Stop Terminal ${tab.ordinal}?`,
        message: "Closing this tab stops its container. Running commands and packages installed in that container will be lost; Project workspace files remain.",
        confirmLabel: "Stop and close",
        tone: "danger",
      });
      if (!approved) return;
      try {
        await api.closeContainerTerminal(tab.session.sessionId);
      } catch (reason) {
        void logCaughtDiagnostic("interface.container_terminal_panel.caught_failure_08", "A handled interface operation failed.", reason, "container_terminal_panel");
        setTabs((current) => current.map((item) => item.key === tab.key && item.kind === "live" ? {
          ...item,
          error: reason instanceof Error ? reason.message : "Could not stop this terminal.",
        } : item));
        return;
      }
    }
    removeTab(tab.key);
    await refreshCapacity(projectAbortRef.current?.signal);
  };

  const cancelImagePreparation = async (tab: StartingTerminalTab) => {
    if (!tab.imagePreparation?.operationId || !tab.imagePreparation.canCancel) return;
    updateStartingTab(tab.key, { phaseDetail: "Cancelling workstation image preparation…" });
    try {
      const control = await api.cancelSetupImage(tab.imagePreparation.operationId);
      updateStartingTab(tab.key, { imagePreparation: control.setup.terminal.imagePreparation });
    } catch (reason) {
      void logCaughtDiagnostic("interface.container_terminal_panel.caught_failure_09", "A handled interface operation failed.", reason, "container_terminal_panel");
      updateStartingTab(tab.key, {
        error: reason instanceof Error ? reason.message : "Could not cancel image preparation.",
      });
    }
  };

  const retryTab = (tab: StartingTerminalTab) => {
    if (launchingKeyRef.current) return;
    void launchTab(tab.key, tab.ordinal, tab.clientIdempotencyKey);
  };

  const activateByKeyboard = (event: ReactKeyboardEvent<HTMLButtonElement>, key: string) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = tabs.findIndex((tab) => tab.key === key);
    let nextIndex = index;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
    const next = tabs[nextIndex];
    if (!next) return;
    setActiveKey(next.key);
    globalThis.requestAnimationFrame?.(() => document.getElementById(`terminal-tab-${next.key}`)?.focus());
  };

  if (initializing || initialError) {
    return <div className="container-terminal-panel">
      <section className="container-terminal-intro">
        <span className="terminal-hero-icon"><SquareTerminal size={23} /></span>
        <div><small>Kali shell</small><h2>Terminal</h2><p><code>kali-linux-headless</code> · disposable · shared <code>/workspace</code></p></div>
        <span className="terminal-boundary"><AlertTriangle size={15} /> Root + network</span>
      </section>
      <section className="terminal-auto-start" aria-live="polite">
        {initialError ? <><SquareTerminal size={27} /><strong>Terminals could not be restored</strong><DiagnosticErrorNotice error={initialError} fallback="The terminal operation could not be completed." compact /><button className="button primary" type="button" onClick={() => setBootstrapAttempt((value) => value + 1)}><RotateCcw size={15} /> Retry</button></> : <><LoaderCircle className="spin" size={27} /><strong>Restoring Project terminals…</strong><p>Nebula is reconnecting active containers before deciding whether to start a new terminal.</p></>}
      </section>
    </div>;
  }

  const launchInProgress = Boolean(launchingKey);
  const canAdd = !launchInProgress && capacity.availableSessions > 0;
  const activeTab = tabs.find((tab) => tab.key === activeKey);

  return <section className="terminal-workspace" aria-label="Project terminals">
    <header className="terminal-tab-bar">
      <div className="terminal-tab-strip" role="tablist" aria-label="Open terminals">
        {tabs.map((tab) => <button
            id={`terminal-tab-${tab.key}`}
            className={`terminal-tab-item terminal-tab-button${activeKey === tab.key ? " active" : ""}`}
            type="button"
            role="tab"
            aria-controls={`terminal-panel-${tab.key}`}
            aria-selected={activeKey === tab.key}
            tabIndex={activeKey === tab.key ? 0 : -1}
            onClick={() => { setActiveKey(tab.key); setOverflowOpen(false); }}
            onKeyDown={(event) => activateByKeyboard(event, tab.key)}
          >
            <span className={`terminal-tab-status ${tabStatus(tab)}`} aria-hidden="true" />
            <span>Terminal {tab.ordinal}</span>
          </button>)}
      </div>
      <div className="terminal-tab-actions">
        <span className="terminal-capacity" title="Active terminal containers across all Projects">{capacity.activeSessions} / {capacity.maxActiveSessions}</span>
        {activeTab && <button className="icon-button subtle terminal-tab-close" type="button" aria-label={`Close Terminal ${activeTab.ordinal}`} onClick={() => void closeTab(activeTab)}><X size={14} /></button>}
        <button className="icon-button subtle terminal-add" type="button" aria-label="New terminal" title={canAdd ? "New terminal" : capacity.availableSessions <= 0 ? "Terminal capacity is full" : "Wait for the current terminal to finish starting"} disabled={!canAdd} onClick={addTerminal}><Plus size={16} /></button>
        <div className="terminal-overflow">
          <button className="icon-button subtle" type="button" aria-label="List all terminals" aria-expanded={overflowOpen} onClick={() => setOverflowOpen((value) => !value)}><ChevronDown size={16} /></button>
          {overflowOpen && <div className="terminal-overflow-menu" role="menu">
            {tabs.length ? tabs.map((tab) => <button type="button" role="menuitem" key={tab.key} className={activeKey === tab.key ? "active" : undefined} onClick={() => { setActiveKey(tab.key); setOverflowOpen(false); }}><span className={`terminal-tab-status ${tabStatus(tab)}`} /><span>Terminal {tab.ordinal}</span></button>) : <span>No open terminals</span>}
          </div>}
        </div>
      </div>
    </header>
    {!tabs.length && <div className="terminal-empty-state"><SquareTerminal size={28} /><strong>No open terminals</strong><p>Start another isolated Kali container for this Project.</p><button className="button primary" type="button" disabled={!canAdd} onClick={addTerminal}><Plus size={15} /> New terminal</button></div>}
    {tabs.map((tab) => <div
      id={`terminal-panel-${tab.key}`}
      className="terminal-tab-panel"
      role="tabpanel"
      aria-labelledby={`terminal-tab-${tab.key}`}
      hidden={activeKey !== tab.key}
      key={tab.key}
    >
      {tab.kind === "starting" ? <StartingTerminalPanel
        engagementName={engagementName}
        tab={tab}
        onCancelPreparation={() => void cancelImagePreparation(tab)}
        onClose={() => removeTab(tab.key)}
        onRetry={() => retryTab(tab)}
      /> : <LiveContainerTerminal
        active={active && activeKey === tab.key}
        api={api}
        auditHealth={auditHealth}
        auditHealthUnavailable={auditHealthUnavailable}
        capturedBy={capturedBy}
        engagementId={engagementId}
        managementError={tab.error}
        onExit={(result) => {
          setTabs((current) => current.map((item) => item.key === tab.key && item.kind === "live" ? { ...item, exit: result } : item));
          void refreshCapacity(projectAbortRef.current?.signal);
        }}
        onNewTerminal={addTerminal}
        onSocketState={(socketState) => setTabs((current) => current.map((item) => item.key === tab.key && item.kind === "live" ? { ...item, socketState } : item))}
        onUploadEvidence={onUploadEvidence}
        runtime={tab.runtime}
        session={tab.session}
      />}
    </div>)}
  </section>;
}
