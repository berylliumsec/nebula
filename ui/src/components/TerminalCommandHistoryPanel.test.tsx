import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { DialogProvider } from "./DialogSystem";
import { TerminalCommandHistoryPanel } from "./TerminalCommandHistoryPanel";

describe("TerminalCommandHistoryPanel", () => {
  it("loads immutable terminal audit records and reveals redacted results lazily", async () => {
    const status = {
      engagementId: "project-1",
      enabled: true,
      captureMode: "selected_tools" as const,
      recordCount: 1,
      recordedOutputCount: 1,
      metadataOnlyCount: 0,
      classificationFailureCount: 0,
      degradedCount: 0,
      truncatedCount: 0,
      auditGapCount: 0,
      capturedOutputBytes: 19,
    };
    const api = {
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue(status),
      listTerminalCommands: vi.fn().mockResolvedValue({
        records: [{
          id: "command-1",
          engagementId: "project-1",
          sessionId: "terminal-1",
          operatorId: "operator-1",
          shellSequence: "2",
          command: "nmap -sV 10.0.0.8",
          commandSha256: "a".repeat(64),
          cwd: "/workspace",
          status: "completed",
          exitCode: 0,
          startedAt: "2026-07-13T20:00:00Z",
          completedAt: "2026-07-13T20:00:01Z",
          occurredAt: "2026-07-13T20:00:01Z",
          rawOutputAvailable: true,
          redactedOutputAvailable: true,
          observedOutputBytes: 19,
          capturedOutputBytes: 19,
          outputSha256: "b".repeat(64),
          outputTruncated: false,
          outputPreview: "PORT STATE SERVICE",
          captureDecision: "selected_tool",
          matchedTools: ["nmap"],
          recordingPolicyRevision: 0,
          runtimeImageDigest: `sha256:${"c".repeat(64)}`,
        }],
        total: 1,
        offset: 0,
        limit: 100,
      }),
      terminalCommandOutput: vi.fn().mockResolvedValue(new Blob(["PORT STATE SERVICE\n80 open http"])),
      terminalRecordingTools: vi.fn().mockResolvedValue({
        engagementId: "project-1",
        inventoryStatus: "verified",
        runtimeImageDigest: `sha256:${"c".repeat(64)}`,
        manifestSha256: "d".repeat(64),
        defaultTools: ["hashcat", "nmap"],
        customTools: [],
        disabledTools: [],
        effectiveTools: ["hashcat", "nmap"],
        revision: 0,
      }),
      updateTerminalRecordingTools: vi.fn(),
    } as unknown as ApiClient;
    const user = userEvent.setup();
    render(<DialogProvider><TerminalCommandHistoryPanel api={api} engagementId="project-1" /></DialogProvider>);

    expect(await screen.findByText("nmap -sV 10.0.0.8")).toBeVisible();
    expect(screen.getByText("Selective capture active")).toBeVisible();
    expect(screen.getByText("Recorded security tools")).toBeVisible();
    expect(screen.getByText("/workspace")).toBeVisible();
    expect(screen.getByText("exit 0")).toBeVisible();

    await user.click(screen.getByRole("button", { name: /nmap -sV/ }));
    expect(await screen.findByText(/80 open http/)).toBeVisible();
    expect(api.terminalCommandOutput).toHaveBeenCalledWith("project-1", "command-1");

    await user.type(screen.getByRole("searchbox", { name: "Search terminal audit commands" }), "nmap");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.listTerminalCommands).toHaveBeenLastCalledWith("project-1", "nmap", 0, 100, expect.any(AbortSignal)));

    expect(screen.getAllByRole("checkbox")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Clear" })).not.toBeInTheDocument();
  });
});
