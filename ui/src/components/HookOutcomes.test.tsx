import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { NativeHookExecution } from "../api/types";
import { HookOutcomes, groupHookOutcomes, hookEventLabel } from "./HookOutcomes";

const run = (id: string, overrides: Partial<NativeHookExecution> = {}): NativeHookExecution => ({
  id, hookId: "routing", eventName: "tool.before", status: "complete", sideEffects: "none", startedAt: "2026-11-21T12:00:00Z", ...overrides,
});
const names: Record<string, string> = { routing: "Search routing", audit: "Audit lifecycle" };
const nameOf = (id: string) => names[id] ?? id;

describe("lifecycle hook outcomes", () => {
  it("labels events as words", () => {
    expect(hookEventLabel("tool.before")).toBe("Before tool");
    expect(hookEventLabel("tool.after")).toBe("After tool");
    expect(hookEventLabel("turn.start")).toBe("Turn start");
    expect(hookEventLabel("session_end")).toBe("Session end");
  });

  it("shares a row for a hook's clean runs and keeps every run that needs attention", () => {
    const rows = groupHookOutcomes([
      ...Array.from({ length: 9 }, (_, index) => run(`before-${index}`)),
      run("after-1", { eventName: "tool.after" }),
      run("failed-1", { status: "failed", error: "exit 2" }),
      run("failed-2", { status: "failed", error: "exit 3" }),
      run("audit-1", { hookId: "audit", status: "interrupted", lateOutcomeStatus: "failed" }),
    ], nameOf);
    expect(rows.map((row) => [row.name, row.event, row.status, row.count])).toEqual([
      ["Search routing", "Before tool", "complete", 9],
      ["Search routing", "After tool", "complete", 1],
      ["Search routing", "Before tool", "failed", 1],
      ["Search routing", "Before tool", "failed", 1],
      ["Audit lifecycle", "Before tool", "interrupted", 1],
    ]);
  });

  it("renders one readable row per group under a collapsed summary", () => {
    render(<HookOutcomes executions={[...Array.from({ length: 9 }, (_, index) => run(`r-${index}`)), run("bad", { status: "failed", error: "exit 2" })]} nameOf={nameOf} open />);
    expect(screen.getByText("Lifecycle hooks · 9/10 completed")).toBeVisible();
    const rows = within(screen.getByRole("list", { name: "Lifecycle hook outcomes" })).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent(/^Search routing\s*Before tool · 9 complete$/);
    expect(within(rows[0]).getByText("Search routing").tagName).toBe("STRONG");
    expect(within(rows[1]).getByRole("alert")).toHaveTextContent("exit 2");
    expect(screen.getByText("Lifecycle hooks · 9/10 completed").closest("details")).toHaveClass("chat-hook-outcomes");
  });
});
