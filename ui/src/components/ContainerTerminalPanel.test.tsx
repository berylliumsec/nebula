import { StrictMode } from "react";
import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ContainerTerminalPanel } from "./ContainerTerminalPanel";

const socketSpies = vi.hoisted(() => ({
  connect: vi.fn(),
  dispose: vi.fn(),
}));

vi.mock("../api/containerTerminal", () => ({
  ContainerTerminalSocket: class {
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
    socketSpies.dispose.mockClear();
  });

  it("does not consume the one-use WebSocket ticket during the StrictMode effect probe", async () => {
    const api = {
      baseUrl: "http://127.0.0.1:8765/api/v1",
      getToken: () => "test-token",
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
          image: `docker.io/kalilinux/kali-rolling@sha256:${"b".repeat(64)}`,
          imageDigest: `sha256:${"b".repeat(64)}`,
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
      }),
    } as unknown as ApiClient;

    render(<StrictMode>
      <ContainerTerminalPanel api={api} engagementId="engagement-1" engagementName="Lab" />
    </StrictMode>);

    await waitFor(() => expect(socketSpies.connect).toHaveBeenCalledTimes(1));
    expect(socketSpies.dispose).toHaveBeenCalledTimes(1);
  });
});
