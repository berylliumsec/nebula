import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ContainerTerminalPanel } from "./ContainerTerminalPanel";

const socketSpies = vi.hoisted(() => ({
  connect: vi.fn(),
  constructed: vi.fn(),
  dispose: vi.fn(),
}));

const terminalSpies = vi.hoisted(() => ({
  keyHandler: undefined as ((event: KeyboardEvent) => boolean) | undefined,
  selection: "",
}));

vi.mock("../api/containerTerminal", () => ({
  ContainerTerminalSocket: class {
    constructor(options: { session: unknown }) {
      socketSpies.constructed(options.session);
    }
    connect = socketSpies.connect;
    dispose = socketSpies.dispose;
    requestClose(): void {}
    resize(): void {}
    sendInput(): void {}
  },
}));

vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fit(): void {}
  },
}));

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 100;
    rows = 30;
    dispose(): void {}
    focus(): void {}
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

describe("ContainerTerminalPanel", () => {
  beforeEach(() => {
    socketSpies.connect.mockClear();
    socketSpies.constructed.mockClear();
    socketSpies.dispose.mockClear();
    terminalSpies.keyHandler = undefined;
    terminalSpies.selection = "";
  });

  it("copies a highlighted terminal selection while preserving Ctrl-C as interrupt without one", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
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
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminal: vi.fn().mockResolvedValue({
        active: true,
        session: {
          sessionId: "terminal-active",
          websocketTicket: "ticket",
          ticketExpiresAt: "2026-07-13T18:00:00Z",
          websocketPath: "/api/v1/container-terminals/terminal-active/ws",
          reconnectGraceSeconds: 600,
          replayMaxBytes: 1_048_576,
          lastSequence: 0,
        },
        runtime,
      }),
    } as unknown as ApiClient;

    render(<ContainerTerminalPanel
      api={api}
      engagementId="engagement-1"
      engagementName="Lab"
      setupTerminalStatus="ready"
    />);
    await waitFor(() => expect(terminalSpies.keyHandler).toBeTypeOf("function"));

    const interrupt = new KeyboardEvent("keydown", { key: "c", ctrlKey: true });
    expect(terminalSpies.keyHandler?.(interrupt)).toBe(true);

    terminalSpies.selection = "nmap output";
    const copy = new KeyboardEvent("keydown", { key: "c", ctrlKey: true, cancelable: true });
    expect(terminalSpies.keyHandler?.(copy)).toBe(false);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("nmap output"));
    expect(copy.defaultPrevented).toBe(true);
  });

  it("waits for runner detection and does not duplicate launch during the StrictMode probe", async () => {
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminal: vi.fn().mockResolvedValue({ active: false }),
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
              operationId: "00000000-0000-4000-8000-000000000001",
              projectId: "engagement-1",
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
      containerTerminalCapabilities: vi.fn().mockResolvedValue({
        ready: true,
      }),
      preflightContainerTerminal: vi.fn().mockResolvedValue({
        allowed: true,
        detail: "approved",
        previewToken: "preview-token",
        previewFingerprint: "preview-fingerprint",
        runtime: {
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
          runnerRuntime: "podman",
          runnerIsolation: "rootless",
          runnerExecutable: "/usr/bin/podman",
          runnerPlatform: "linux/amd64",
        },
      }),
      startContainerTerminal: vi.fn().mockResolvedValue({
        sessionId: "terminal-1",
        websocketTicket: "one-use-ticket",
        ticketExpiresAt: "2026-07-13T18:00:00Z",
        websocketPath: "/api/v1/container-terminals/terminal-1/ws",
        reconnectGraceSeconds: 600,
        replayMaxBytes: 1_048_576,
        lastSequence: 0,
      }),
    } as unknown as ApiClient;

    render(<StrictMode>
      <ContainerTerminalPanel
        api={api}
        engagementId="engagement-1"
        engagementName="Lab"
        setupTerminalStatus="detecting_runner"
        setupTerminalDetail="Checking supported local container runtimes."
      />
    </StrictMode>);

    expect(screen.getByText("Detecting a supported local container runtime…")).toBeVisible();
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(1));
    expect(api.recoverContainerTerminal).toHaveBeenCalledTimes(1);
    expect(api.setupStatus).toHaveBeenCalledTimes(1);
    expect(api.prepareSetupImage).toHaveBeenCalledWith("engagement-1", expect.any(AbortSignal));
    expect(api.containerTerminalCapabilities).toHaveBeenCalledTimes(1);
    expect(api.startContainerTerminal).toHaveBeenCalledTimes(1);
    expect(socketSpies.dispose).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Unrestricted outbound · root · writable/)).toBeVisible();
    expect(screen.getByText(/Bridge networking can reach the public Internet/)).toBeVisible();
    expect(screen.getByText(/Installed baseline:/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Screenshot" })).toBeVisible();
    expect(screen.getByTitle(`docker.io/kalilinux/kali-rolling@sha256:${"b".repeat(64)}`)).toBeVisible();
  });

  it("recovers the same active terminal after a top-level unmount without starting another", async () => {
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
    const recoveredSession = (ticket: string) => ({
      sessionId: "terminal-active",
      websocketTicket: ticket,
      ticketExpiresAt: "2026-07-13T18:00:00Z",
      websocketPath: "/api/v1/container-terminals/terminal-active/ws",
      reconnectGraceSeconds: 600,
      replayMaxBytes: 1_048_576,
      lastSequence: 0,
    });
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
      recoverContainerTerminal: vi.fn()
        .mockResolvedValueOnce({
          active: true,
          session: recoveredSession("fresh-ticket-1"),
          runtime,
        })
        .mockResolvedValueOnce({
          active: true,
          session: recoveredSession("fresh-ticket-2"),
          runtime,
        }),
      setupStatus: vi.fn(),
      preflightContainerTerminal: vi.fn(),
      startContainerTerminal: vi.fn(),
    } as unknown as ApiClient;

    const first = render(<ContainerTerminalPanel
      api={api}
      engagementId="engagement-1"
      engagementName="Lab"
      setupTerminalStatus="ready"
    />);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(1));
    first.unmount();

    render(<ContainerTerminalPanel
      api={api}
      engagementId="engagement-1"
      engagementName="Lab"
      setupTerminalStatus="ready"
    />);
    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(2));

    expect(api.recoverContainerTerminal).toHaveBeenCalledTimes(2);
    expect(api.setupStatus).not.toHaveBeenCalled();
    expect(api.preflightContainerTerminal).not.toHaveBeenCalled();
    expect(api.startContainerTerminal).not.toHaveBeenCalled();
    expect(socketSpies.constructed.mock.calls.map(([session]) => (
      session as { websocketTicket: string }
    ).websocketTicket)).toEqual(["fresh-ticket-1", "fresh-ticket-2"]);
  });
});
