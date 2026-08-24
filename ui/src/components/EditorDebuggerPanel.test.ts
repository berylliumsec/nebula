import type { DebugProtocol } from "@vscode/debugprotocol";
import { describe, expect, it } from "vitest";
import { buildDebugSnapshot } from "./EditorDebuggerPanel";

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
