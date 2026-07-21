import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { OperatorExecution } from "../api/types";
import { ExecutionHistory } from "./ExecutionHistory";

const running: OperatorExecution = {
  id: "execution-1",
  engagementId: "project-1",
  operatorId: "operator-1",
  origin: { kind: "assistant_message", messageId: "message-1", blockOrdinal: 0 },
  language: "bash",
  sourceSha256: "a".repeat(64),
  sourceArtifactId: "artifact-1",
  sourcePreview: "printf ready",
  runtime: {
    language: "bash",
    interpreter: "/bin/bash",
    arguments: ["/workspace/run.sh"],
    image: "nebula-kali:latest",
    runtimeDigest: "sha256:runtime",
    runnerProfileId: "runner-1",
    runnerProfileRevision: 1,
    runnerRuntime: "docker",
    runnerIsolation: "container",
    runnerExecutable: "docker",
    runnerPlatform: "linux/amd64",
  },
  network: { mode: "none", ports: [], resolvedAddresses: [] },
  limits: { timeoutSeconds: 30, memoryMb: 512, cpuCount: 1, pids: 64, outputBytesPerStream: 262_144 },
  workspace: "/workspace",
  policyDecision: "approved",
  status: "running",
  queuedAt: "2026-07-18T10:00:00Z",
  startedAt: "2026-07-18T10:00:01Z",
  outputTruncated: false,
  workspaceChanges: [],
};

const completed: OperatorExecution = {
  ...running,
  status: "completed",
  completedAt: "2026-07-18T10:00:03Z",
  exitCode: 0,
};

function renderHistory(api: Partial<ApiClient>) {
  return render(<ExecutionHistory
    api={api as ApiClient}
    engagementId="project-1"
    onRerun={vi.fn()}
    providers={[]}
    onChatAttached={vi.fn()}
  />);
}

afterEach(() => {
  vi.useRealTimers();
});

describe("ExecutionHistory active updates", () => {
  it("polls active work and loads selected output as soon as it completes", async () => {
    vi.useFakeTimers();
    const listExecutions = vi.fn()
      .mockResolvedValueOnce({ items: [running], total: 1 })
      .mockResolvedValue({ items: [completed], total: 1 });
    const executionOutput = vi.fn().mockImplementation((_id, stream) => Promise.resolve({
      text: stream === "stdout" ? "ready\n" : "",
      totalBytes: stream === "stdout" ? 6 : 0,
      nextOffset: stream === "stdout" ? 6 : 0,
    }));
    renderHistory({ listExecutions, executionOutput });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(listExecutions).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /bash · running/i }));
    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(listExecutions).toHaveBeenCalledTimes(2);
    expect(executionOutput).toHaveBeenCalledTimes(2);
    expect(screen.getByText("ready", { exact: false, selector: "pre" })).toBeVisible();
  });

  it("pauses after a refresh failure and makes recovery explicit and retryable", async () => {
    vi.useFakeTimers();
    const listExecutions = vi.fn()
      .mockResolvedValueOnce({ items: [running], total: 1 })
      .mockRejectedValueOnce(new Error("Core is temporarily unavailable"))
      .mockResolvedValue({ items: [completed], total: 1 });
    renderHistory({ listExecutions });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("Updates paused.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Retry updates" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(listExecutions).toHaveBeenCalledTimes(3);
    expect(screen.queryByText("Updates paused.")).not.toBeInTheDocument();
  });

  it("treats an invalid local date range as form validation, not an incident", async () => {
    renderHistory({ listExecutions: vi.fn().mockResolvedValue({ items: [], total: 0 }) });
    await screen.findByText("No executions match");

    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-07-20" } });
    fireEvent.change(screen.getByLabelText("Through"), { target: { value: "2026-07-19" } });

    expect(await screen.findByText(/Through date must be the same as or later/)).toBeVisible();
    expect(screen.queryByRole("link", { name: /diagnostics/i })).not.toBeInTheDocument();
  });
});
