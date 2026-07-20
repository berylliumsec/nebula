import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { DialogProvider } from "./DialogSystem";
import { ContainerTerminalPanel } from "./ContainerTerminalPanel";

const socketSpies = vi.hoisted(() => ({
  connect: vi.fn(),
  constructed: vi.fn(),
  dispose: vi.fn(),
  resize: vi.fn(),
}));

const terminalSpies = vi.hoisted(() => ({
  fit: vi.fn(),
  focus: vi.fn(),
  keyHandler: undefined as ((event: KeyboardEvent) => boolean) | undefined,
  options: undefined as Record<string, unknown> | undefined,
  selection: "",
}));

vi.mock("../api/containerTerminal", () => ({
  ContainerTerminalSocket: class {
    constructor(private readonly options: { session: unknown; onState?: (state: string) => void; onExit?: (result: { outcome: string }) => void }) {
      socketSpies.constructed(options.session);
    }
    connect = () => {
      socketSpies.connect();
      this.options.onState?.("ready");
    };
    dispose = socketSpies.dispose;
    requestClose = () => this.options.onExit?.({ outcome: "closed" });
    resize = socketSpies.resize;
    sendInput(): void {}
  },
}));

vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fit = terminalSpies.fit;
  },
}));

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 100;
    rows = 30;
    constructor(options: Record<string, unknown>) {
      terminalSpies.options = options;
    }
    dispose(): void {}
    focus = terminalSpies.focus;
    loadAddon(): void {}
    open(): void {}
    write(): void {}
    attachCustomKeyEventHandler(handler: (event: KeyboardEvent) => boolean): void {
      terminalSpies.keyHandler = handler;
    }
    getSelection(): string {
      return terminalSpies.selection;
    }
    hasSelection(): boolean {
      return terminalSpies.selection.length > 0;
    }
    onSelectionChange(): { dispose: () => void } {
      return { dispose: vi.fn() };
    }
    onData(): { dispose: () => void } {
      return { dispose: vi.fn() };
    }
    onResize(): { dispose: () => void } {
      return { dispose: vi.fn() };
    }
  },
}));

const runtime = {
  sourceImage: "docker.io/kalilinux/kali-rolling:latest",
  baseImage: `docker.io/kalilinux/kali-rolling@sha256:${"b".repeat(64)}`,
  baseImageDigest: `sha256:${"b".repeat(64)}`,
  image: `sha256:${"c".repeat(64)}`,
  imageDigest: `sha256:${"c".repeat(64)}`,
  installedPackages: ["kali-linux-headless", "iputils-ping"],
  interpreter: "/bin/bash",
  arguments: ["--noprofile", "--norc", "-i"],
  runnerProfileId: "local",
  runnerProfileRevision: 1,
  runnerRuntime: "podman" as const,
  runnerIsolation: "rootless",
  runnerExecutable: "/usr/bin/podman",
  runnerPlatform: "linux/amd64",
};

const session = (id: string, ticket = `ticket-${id}`, createdAt = "2026-07-15T12:00:00Z") => ({
  sessionId: id,
  createdAt,
  websocketTicket: ticket,
  ticketExpiresAt: "2026-07-15T18:00:00Z",
  websocketPath: `/api/v1/container-terminals/${id}/ws`,
  reconnectGraceSeconds: 600,
  replayMaxBytes: 1_048_576,
  lastSequence: 0,
});

const capacity = (activeSessions: number) => ({
  activeSessions,
  availableSessions: 32 - activeSessions,
  maxActiveSessions: 32,
});

function renderPanel(api: ApiClient, strict = false, active = true) {
  const panel = <DialogProvider><ContainerTerminalPanel
    active={active}
    api={api}
    engagementId="engagement-1"
    engagementName="Lab"
    setupTerminalStatus="ready"
  /></DialogProvider>;
  return render(strict ? <StrictMode>{panel}</StrictMode> : panel);
}

describe("ContainerTerminalPanel", () => {
  beforeEach(() => {
    socketSpies.connect.mockClear();
    socketSpies.constructed.mockClear();
    socketSpies.dispose.mockClear();
    socketSpies.resize.mockClear();
    terminalSpies.fit.mockClear();
    terminalSpies.focus.mockClear();
    terminalSpies.keyHandler = undefined;
    terminalSpies.options = undefined;
    terminalSpies.selection = "";
  });

  it("keeps an accessible progress indicator visible while Kali preparation is indeterminate", async () => {
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({ sessions: [] }),
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(0)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
      setupStatus: vi.fn().mockResolvedValue({
        core: { status: "ready" },
        scratchProjectId: "engagement-1",
        terminal: {
          status: "preparing_image",
          runnerProfileId: "local",
          candidates: [],
          imagePreparation: {
            phase: "preparing_image",
            progressIndeterminate: true,
            canCancel: true,
            canRetry: false,
            detail: "Downloading and verifying the Kali runtime.",
          },
        },
        assistant: { status: "needs_model" },
      }),
    } as unknown as ApiClient;

    renderPanel(api);

    expect(await screen.findByText("Preparing Kali runtime")).toBeVisible();
    const progress = screen.getByRole("progressbar", { name: "Kali terminal startup progress" });
    expect(progress).toBeVisible();
    expect(progress).toHaveAttribute("aria-valuetext", "Preparing Kali runtime");
    expect(progress).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByRole("button", { name: "Cancel" })).toBeVisible();
  });

  it("copies highlighted terminal text and polls Project audit health only once", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({
        sessions: [
          { session: session("terminal-1"), runtime },
          { session: session("terminal-2", "ticket-2", "2026-07-15T12:01:00Z"), runtime },
        ],
      }),
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(2)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({
        engagementId: "engagement-1",
        enabled: true,
        captureMode: "selected_tools",
        recordCount: 1,
        recordedOutputCount: 1,
        metadataOnlyCount: 0,
        classificationFailureCount: 0,
        degradedCount: 0,
        truncatedCount: 0,
        auditGapCount: 1,
        capturedOutputBytes: 0,
      }),
    } as unknown as ApiClient;

    renderPanel(api);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(2));
    expect(await screen.findAllByText(/1 terminal audit warning detected/)).toHaveLength(2);
    expect(api.terminalCommandHistoryStatus).toHaveBeenCalledTimes(1);

    const interrupt = new KeyboardEvent("keydown", { key: "c", ctrlKey: true });
    expect(terminalSpies.keyHandler?.(interrupt)).toBe(true);
    terminalSpies.selection = "nmap output";
    const copy = new KeyboardEvent("keydown", { key: "c", ctrlKey: true, cancelable: true });
    expect(terminalSpies.keyHandler?.(copy)).toBe(false);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("nmap output"));
    expect(copy.defaultPrevented).toBe(true);
    expect(terminalSpies.options).toMatchObject({
      cursorBlink: false,
      cursorInactiveStyle: "block",
      cursorStyle: "block",
      theme: expect.objectContaining({
        cursor: "#b8ffe3",
        cursorAccent: "#071017",
        selectionBackground: "#168bd2",
        selectionInactiveBackground: "#126fa8",
        selectionForeground: "#ffffff",
      }),
    });
    const focusCalls = terminalSpies.focus.mock.calls.length;
    fireEvent.pointerDown(screen.getAllByLabelText("Terminal output").at(-1)!);
    expect(terminalSpies.focus).toHaveBeenCalledTimes(focusCalls + 1);
  });

  it("allows the network boundary notice to be dismissed", async () => {
    const user = userEvent.setup();
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({ sessions: [
        { session: session("terminal-1"), runtime },
      ] }),
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(1)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
    } as unknown as ApiClient;

    renderPanel(api);
    expect(await screen.findByText(/Bridge networking is permitted, not guaranteed/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Dismiss network boundary notice" }));
    expect(screen.queryByText(/Bridge networking is permitted, not guaranteed/)).not.toBeInTheDocument();
  });

  it("prepares and starts exactly one initial terminal during the StrictMode probe", async () => {
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({ sessions: [] }),
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(1)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
      setupStatus: vi.fn().mockResolvedValue({
        core: { status: "ready" },
        scratchProjectId: "engagement-1",
        terminal: {
          status: "ready",
          runnerProfileId: "local",
          candidates: [],
          imagePreparation: {
            phase: "not_started",
            progressIndeterminate: false,
            canCancel: false,
            canRetry: false,
          },
        },
        assistant: { status: "needs_model" },
      }),
      prepareSetupImage: vi.fn().mockResolvedValue({
        operation: "image_preparation",
        accepted: true,
        idempotent: false,
        operationId: "00000000-0000-4000-8000-000000000001",
        setup: {
          core: { status: "ready" },
          scratchProjectId: "engagement-1",
          terminal: {
            status: "ready",
            runnerProfileId: "local",
            candidates: [],
            imagePreparation: {
              phase: "ready",
              progressPercent: 100,
              progressIndeterminate: false,
              canCancel: false,
              canRetry: false,
              imageDigest: `sha256:${"c".repeat(64)}`,
            },
          },
          assistant: { status: "needs_model" },
        },
      }),
      containerTerminalCapabilities: vi.fn().mockResolvedValue({ ready: true }),
      preflightContainerTerminal: vi.fn().mockResolvedValue({
        allowed: true,
        detail: "approved",
        previewToken: "preview-token",
        previewFingerprint: "preview-fingerprint",
        runtime,
      }),
      startContainerTerminal: vi.fn().mockResolvedValue(session("terminal-1")),
    } as unknown as ApiClient;

    renderPanel(api, true);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(1));
    expect(api.recoverContainerTerminals).toHaveBeenCalledTimes(1);
    expect(api.prepareSetupImage).toHaveBeenCalledTimes(1);
    expect(api.startContainerTerminal).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("tab", { name: /Terminal 1/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText(/Outbound bridge permitted · root · writable/)).not.toBeInTheDocument();
    expect(screen.queryByText("Selective audit active")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Screenshot" })).toBeVisible();
  });

  it("recovers every terminal and keeps sockets mounted while switching tabs", async () => {
    const user = userEvent.setup();
    const recoverContainerTerminals = vi.fn()
      .mockResolvedValueOnce({ sessions: [
        { session: session("terminal-1", "fresh-1"), runtime },
        { session: session("terminal-2", "fresh-2", "2026-07-15T12:01:00Z"), runtime },
      ] })
      .mockResolvedValueOnce({ sessions: [
        { session: session("terminal-1", "fresh-3"), runtime },
        { session: session("terminal-2", "fresh-4", "2026-07-15T12:01:00Z"), runtime },
      ] });
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals,
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(2)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
      startContainerTerminal: vi.fn(),
      preflightContainerTerminal: vi.fn(),
    } as unknown as ApiClient;

    const first = renderPanel(api);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("tab", { name: /Terminal 2/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: "Screenshot" }).closest("[role='tabpanel']"))
      .toHaveAttribute("id", "terminal-panel-terminal-2");
    await user.click(screen.getByRole("tab", { name: /Terminal 1/ }));
    expect(screen.getByRole("tab", { name: /Terminal 1/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: "Screenshot" }).closest("[role='tabpanel']"))
      .toHaveAttribute("id", "terminal-panel-terminal-1");
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /Terminal 2/ })).toHaveAttribute("aria-selected", "true");
    expect(socketSpies.dispose).not.toHaveBeenCalled();
    expect(terminalSpies.fit).toHaveBeenCalled();
    first.unmount();

    renderPanel(api);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(4));
    expect(recoverContainerTerminals).toHaveBeenCalledTimes(2);
    expect(api.startContainerTerminal).not.toHaveBeenCalled();
    expect(socketSpies.constructed.mock.calls.map(([value]) => (
      value as { websocketTicket: string }
    ).websocketTicket)).toEqual(["fresh-1", "fresh-2", "fresh-3", "fresh-4"]);
  });

  it("does not fit or resize a terminal while its workbench view is inactive", async () => {
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({ sessions: [
        { session: session("terminal-hidden"), runtime },
      ] }),
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(1)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
    } as unknown as ApiClient;

    renderPanel(api, false, false);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(1));
    expect(terminalSpies.fit).not.toHaveBeenCalled();
    expect(socketSpies.resize).not.toHaveBeenCalled();
  });

  it("confirms a running tab close and stops only that session", async () => {
    const user = userEvent.setup();
    const closeContainerTerminal = vi.fn().mockResolvedValue(undefined);
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({ sessions: [
        { session: session("terminal-1"), runtime },
        { session: session("terminal-2", "ticket-2", "2026-07-15T12:01:00Z"), runtime },
      ] }),
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(2)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
      closeContainerTerminal,
    } as unknown as ApiClient;

    renderPanel(api);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("tab", { name: /Terminal 1/ }));
    await user.click(screen.getByRole("button", { name: "Close Terminal 1" }));
    expect(screen.getByRole("dialog", { name: "Stop Terminal 1?" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Stop and close" }));
    await waitFor(() => expect(closeContainerTerminal).toHaveBeenCalledWith("terminal-1"));
    expect(screen.queryByRole("tab", { name: /Terminal 1/ })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Terminal 2/ })).toBeVisible();
  });

  it("preserves a running tab when close is cancelled or stopping fails", async () => {
    const user = userEvent.setup();
    const closeContainerTerminal = vi.fn().mockRejectedValue(new Error("container runtime refused stop"));
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({ sessions: [
        { session: session("terminal-1"), runtime },
      ] }),
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(1)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
      closeContainerTerminal,
    } as unknown as ApiClient;

    renderPanel(api);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Close Terminal 1" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(closeContainerTerminal).not.toHaveBeenCalled();
    expect(screen.getByRole("tab", { name: /Terminal 1/ })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Close Terminal 1" }));
    await user.click(screen.getByRole("button", { name: "Stop and close" }));
    expect(await screen.findByText("container runtime refused stop")).toBeVisible();
    expect(screen.getByRole("tab", { name: /Terminal 1/ })).toBeVisible();
  });

  it("adds a fresh terminal tab without remounting the existing socket", async () => {
    const user = userEvent.setup();
    const containerTerminalCapacity = vi.fn()
      .mockResolvedValueOnce(capacity(1))
      .mockResolvedValueOnce(capacity(2));
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({ sessions: [
        { session: session("terminal-1"), runtime },
      ] }),
      containerTerminalCapacity,
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
      containerTerminalCapabilities: vi.fn().mockResolvedValue({ ready: true }),
      preflightContainerTerminal: vi.fn().mockResolvedValue({
        allowed: true,
        detail: "approved",
        previewToken: "preview-token-2",
        previewFingerprint: "preview-fingerprint-2",
        runtime,
      }),
      startContainerTerminal: vi.fn().mockResolvedValue(session("terminal-2", "ticket-2", "2026-07-15T12:01:00Z")),
    } as unknown as ApiClient;

    renderPanel(api);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "New terminal" }));
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(2));

    expect(api.startContainerTerminal).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("tab", { name: /Terminal 2/ })).toHaveAttribute("aria-selected", "true");
    expect(socketSpies.dispose).not.toHaveBeenCalled();
    expect(await screen.findByText("2 / 32")).toBeVisible();
  });

  it("retains a failed provisional tab and reuses its idempotency key on Retry", async () => {
    const user = userEvent.setup();
    const startContainerTerminal = vi.fn()
      .mockRejectedValueOnce(new Error("terminal capacity is currently full"))
      .mockResolvedValueOnce(session("terminal-2", "ticket-2", "2026-07-15T12:01:00Z"));
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({ sessions: [
        { session: session("terminal-1"), runtime },
      ] }),
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(1)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
      containerTerminalCapabilities: vi.fn().mockResolvedValue({ ready: true }),
      preflightContainerTerminal: vi.fn().mockResolvedValue({
        allowed: true,
        detail: "approved",
        previewToken: "preview-token-2",
        previewFingerprint: "preview-fingerprint-2",
        runtime,
      }),
      startContainerTerminal,
    } as unknown as ApiClient;

    renderPanel(api);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "New terminal" }));
    expect(await screen.findByText("terminal capacity is currently full")).toBeVisible();
    expect(screen.getByRole("tab", { name: /Terminal 2/ })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(2));

    expect(startContainerTerminal).toHaveBeenCalledTimes(2);
    expect(startContainerTerminal.mock.calls[0]?.[2]).toBe(startContainerTerminal.mock.calls[1]?.[2]);
  });

  it("exposes every tab through the overflow menu", async () => {
    const user = userEvent.setup();
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({ sessions: [
        { session: session("terminal-1"), runtime },
        { session: session("terminal-2", "ticket-2", "2026-07-15T12:01:00Z"), runtime },
        { session: session("terminal-3", "ticket-3", "2026-07-15T12:02:00Z"), runtime },
      ] }),
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(3)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
    } as unknown as ApiClient;

    renderPanel(api);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(3));
    await user.click(screen.getByRole("button", { name: "List all terminals" }));
    const menu = screen.getByRole("menu");
    expect(menu).toBeVisible();
    await user.click(screen.getByRole("menuitem", { name: /Terminal 1/ }));
    expect(screen.getByRole("tab", { name: /Terminal 1/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("disables New Terminal when the global capacity is full", async () => {
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminals: vi.fn().mockResolvedValue({ sessions: [
        { session: session("terminal-1"), runtime },
      ] }),
      containerTerminalCapacity: vi.fn().mockResolvedValue(capacity(32)),
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({}),
    } as unknown as ApiClient;

    renderPanel(api);
    await waitFor(() => expect(screen.getByText("32 / 32")).toBeVisible());
    expect(screen.getByRole("button", { name: "New terminal" })).toBeDisabled();
  });
});
