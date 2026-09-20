import type { DebugProtocol } from "@vscode/debugprotocol";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { buildDebugSnapshot, EditorDebuggerPanel } from "./EditorDebuggerPanel";

interface FakeTransport {
  options: { onEvent(event: DebugProtocol.Event): void; onState?(state: string, detail?: string): void };
  request: ReturnType<typeof vi.fn>;
}

const transportMocks = vi.hoisted(() => ({ instances: [] as FakeTransport[] }));

vi.mock("../api/debugger", () => ({
  DebugTransport: class {
    request = vi.fn(async (command: string) => ({ seq: 1, type: "response", request_seq: 1, success: true, command, body: {} }));
    connect = vi.fn(async () => { this.options.onState?.("ready"); });
    close = vi.fn();
    constructor(public options: FakeTransport["options"]) {
      transportMocks.instances.push(this as unknown as FakeTransport);
    }
  },
}));

function debuggerApi(): ApiClient {
  return {
    baseUrl: "http://127.0.0.1:8765/api/v1",
    getToken: () => "test-token",
    startDebugSession: vi.fn(async () => ({
      sessionId: "debug-session-1",
      websocketPath: "/debug/debug-session-1",
      websocketTicket: "ticket-1",
      protocol: "nebula.debug.v1",
      path: "tool.py",
      sourceSha256: "a".repeat(64),
      imageDigest: `sha256:${"b".repeat(64)}`,
      workspaceAccess: "read-only",
      network: "none",
      expiresAt: "2026-07-18T11:00:00Z",
    })),
  } as unknown as ApiClient;
}

describe("EditorDebuggerPanel breakpoints", () => {
  beforeEach(() => {
    transportMocks.instances.length = 0;
  });

  it("re-sends breakpoints toggled after launch to the running adapter", async () => {
    const props = {
      api: debuggerApi(),
      engagementId: "project-1",
      path: "tool.py",
      expectedSha256: "a".repeat(64),
      dirty: false,
      cursorLine: 1,
      onToggleBreakpoint: vi.fn(),
      onReveal: vi.fn(),
      onClose: vi.fn(),
    };
    const { rerender } = render(<EditorDebuggerPanel {...props} breakpoints={[10]} />);

    fireEvent.click(screen.getByRole("button", { name: "Start isolated debugger" }));
    await waitFor(() => expect(transportMocks.instances).toHaveLength(1));
    const transport = transportMocks.instances[0];
    await waitFor(() => expect(transport.request).toHaveBeenCalledWith("launch", expect.objectContaining({ program: "/workspace/tool.py" })));

    act(() => transport.options.onEvent({ seq: 2, type: "event", event: "initialized" }));
    await waitFor(() => expect(transport.request).toHaveBeenCalledWith("configurationDone", undefined));
    const setBreakpointCalls = () => transport.request.mock.calls.filter(([command]) => command === "setBreakpoints");
    expect(setBreakpointCalls()).toHaveLength(1);
    expect(setBreakpointCalls()[0][1]).toMatchObject({ source: { path: "/workspace/tool.py" }, breakpoints: [{ line: 10 }] });

    // The operator clicks the gutter on line 40 while the debuggee is running.
    rerender(<EditorDebuggerPanel {...props} breakpoints={[10, 40]} />);
    await waitFor(() => expect(setBreakpointCalls()).toHaveLength(2));
    expect(setBreakpointCalls()[1][1]).toMatchObject({ breakpoints: [{ line: 10 }, { line: 40 }] });

    // An unrelated re-render with the same breakpoints does not spam the adapter.
    rerender(<EditorDebuggerPanel {...props} breakpoints={[10, 40]} cursorLine={2} />);
    await act(async () => { await Promise.resolve(); });
    expect(setBreakpointCalls()).toHaveLength(2);

    // Removing a breakpoint after launch is sent too, so it stops firing.
    rerender(<EditorDebuggerPanel {...props} breakpoints={[40]} />);
    await waitFor(() => expect(setBreakpointCalls()).toHaveLength(3));
    expect(setBreakpointCalls()[2][1]).toMatchObject({ breakpoints: [{ line: 40 }] });

    // Once the session has ended nothing is sent.
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(transport.request).toHaveBeenCalledWith("disconnect", expect.anything()));
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled());
    rerender(<EditorDebuggerPanel {...props} breakpoints={[40, 50]} />);
    await act(async () => { await Promise.resolve(); });
    expect(setBreakpointCalls()).toHaveLength(3);
  });
});

describe("buildDebugSnapshot", () => {
  it("creates a bounded, typed assistant context with exact security boundaries", () => {
    const variables = Array.from({ length: 101 }, (_, index) => ({
      name: `value_${index}`,
      value: "x".repeat(1200),
      variablesReference: 0,
    })) as DebugProtocol.Variable[];

    const context = buildDebugSnapshot({
      sessionId: "debug-session-1",
      path: "research/parser.py",
      sourceSha256: "a".repeat(64),
      imageDigest: `sha256:${"b".repeat(64)}`,
      state: "stopped",
      stack: [{
        id: 1,
        name: "parse_target",
        source: { path: "/workspace/research/parser.py" },
        line: 42,
        column: 3,
      }],
      variables: [{ name: "Locals", variables }],
      output: ["prefix\n", "o".repeat(12_000)],
    });

    expect(context).toMatchObject({
      sourceKind: "debug_snapshot",
      sourceId: "debug-session-1",
      sourceLabel: "Debugger: research/parser.py",
      truncated: true,
    });
    expect(context.text.length).toBeLessThanOrEqual(65_536);
    const snapshot = JSON.parse(context.text);
    expect(snapshot).toMatchObject({
      schema: "nebula.debug-snapshot/v1",
      source: { path: "research/parser.py", sha256: "a".repeat(64) },
      runtime: {
        imageDigest: `sha256:${"b".repeat(64)}`,
        workspaceAccess: "read-only",
        networkAccess: "none",
      },
      session: { id: "debug-session-1", state: "stopped" },
      stack: [{ name: "parse_target", path: "research/parser.py", line: 42, column: 3 }],
    });
    expect(snapshot.output.length).toBeLessThanOrEqual(10_000);
  });
});
