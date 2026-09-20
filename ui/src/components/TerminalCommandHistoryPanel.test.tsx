import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { DialogProvider } from "./DialogSystem";
import { TerminalCommandHistoryPanel } from "./TerminalCommandHistoryPanel";

vi.mock("./ResourceActionMenu", () => ({ ResourceActionMenu: () => null }));

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

  it("keeps the raw download object URL alive until the browser has started the download", async () => {
    const record = {
      id: "command-raw",
      engagementId: "project-1",
      sessionId: "terminal-1",
      operatorId: "operator-1",
      shellSequence: "4",
      command: "nmap -sV 10.0.0.9",
      commandSha256: "a".repeat(64),
      cwd: "/workspace",
      status: "completed",
      exitCode: 0,
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
    };
    const api = {
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({
        engagementId: "project-1", enabled: true, captureMode: "selected_tools", recordCount: 1, recordedOutputCount: 1,
        metadataOnlyCount: 0, classificationFailureCount: 0, degradedCount: 0, truncatedCount: 0, auditGapCount: 0, capturedOutputBytes: 19,
      }),
      listTerminalCommands: vi.fn().mockResolvedValue({ records: [record], total: 1, offset: 0, limit: 100 }),
      terminalCommandOutput: vi.fn().mockResolvedValue(new Blob(["PORT STATE SERVICE\n80 open http"])),
      terminalRecordingTools: vi.fn().mockResolvedValue({
        engagementId: "project-1", inventoryStatus: "unavailable", defaultTools: [], customTools: [], disabledTools: [], effectiveTools: [], revision: 0,
      }),
      updateTerminalRecordingTools: vi.fn(),
    } as unknown as ApiClient;
    const createObjectURL = vi.fn(() => "blob:terminal-raw");
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const user = userEvent.setup();
    render(<DialogProvider><TerminalCommandHistoryPanel api={api} engagementId="project-1" /></DialogProvider>);

    await user.click(await screen.findByRole("button", { name: /nmap -sV/ }));
    await user.click(await screen.findByRole("button", { name: "Raw" }));
    await user.click(await screen.findByRole("button", { name: "Download raw result" }));
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    expect(api.terminalCommandOutput).toHaveBeenLastCalledWith("project-1", "command-raw", true);
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(revokeObjectURL).not.toHaveBeenCalled();
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:terminal-raw"), { timeout: 2_500 });
    click.mockRestore();
  });

  it("saves custom names without an image catalog and keeps metadata-only output actions unavailable", async () => {
    const catalog = {
      engagementId: "project-1",
      inventoryStatus: "unavailable" as const,
      defaultTools: [],
      customTools: [],
      disabledTools: [],
      effectiveTools: [],
      revision: 0,
    };
    const api = {
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({
        engagementId: "project-1",
        enabled: true,
        captureMode: "selected_tools",
        recordCount: 1,
        recordedOutputCount: 0,
        metadataOnlyCount: 1,
        classificationFailureCount: 0,
        degradedCount: 0,
        truncatedCount: 0,
        auditGapCount: 0,
        capturedOutputBytes: 0,
      }),
      listTerminalCommands: vi.fn().mockResolvedValue({
        records: [{
          id: "command-metadata",
          engagementId: "project-1",
          sessionId: "terminal-1",
          operatorId: "operator-1",
          shellSequence: "3",
          command: "ls -la",
          cwd: "/workspace",
          status: "completed",
          exitCode: 0,
          occurredAt: "2026-07-13T20:00:01Z",
          rawOutputAvailable: false,
          redactedOutputAvailable: false,
          observedOutputBytes: 0,
          capturedOutputBytes: 0,
          outputTruncated: false,
          outputPreview: "",
          captureDecision: "not_selected",
          matchedTools: [],
          recordingPolicyRevision: 0,
          runtimeImageDigest: `sha256:${"c".repeat(64)}`,
        }],
        total: 1,
        offset: 0,
        limit: 100,
      }),
      terminalCommandOutput: vi.fn(),
      terminalRecordingTools: vi.fn().mockResolvedValue(catalog),
      updateTerminalRecordingTools: vi.fn().mockResolvedValue({
        ...catalog,
        customTools: ["session-scanner"],
        effectiveTools: ["session-scanner"],
        revision: 1,
      }),
    } as unknown as ApiClient;
    const user = userEvent.setup();
    render(<DialogProvider><TerminalCommandHistoryPanel api={api} engagementId="project-1" /></DialogProvider>);

    expect(await screen.findByText(/verified Kali catalog is not available/)).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "Custom executable name" }), "session-scanner");
    await user.click(screen.getByRole("button", { name: "Add custom" }));
    expect(screen.getByRole("checkbox", { name: /session-scanner/ })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Save tools" }));
    await waitFor(() => expect(api.updateTerminalRecordingTools).toHaveBeenCalledWith(
      "project-1",
      {
        customTools: ["session-scanner"],
        disabledTools: [],
        expectedRevision: 0,
        expectedManifestSha256: undefined,
      },
    ));

    await user.click(screen.getByRole("button", { name: /ls -la/ }));
    expect(screen.getByText("Output was not retained for this metadata-only record.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Raw" })).toBeDisabled();
    expect(api.terminalCommandOutput).not.toHaveBeenCalled();
  });

  it("ignores a Load more page that lands after the list was reloaded for another project", async () => {
    const record = (id: string, command: string) => ({
      id,
      engagementId: "project-1",
      sessionId: "terminal-1",
      operatorId: "operator-1",
      shellSequence: "1",
      command,
      commandSha256: "a".repeat(64),
      cwd: "/workspace",
      status: "completed",
      exitCode: 0,
      occurredAt: "2026-07-13T20:00:01Z",
      rawOutputAvailable: false,
      redactedOutputAvailable: false,
      observedOutputBytes: 0,
      capturedOutputBytes: 0,
      outputSha256: "b".repeat(64),
      outputTruncated: false,
      outputPreview: "",
      captureDecision: "metadata_only",
      matchedTools: [],
      recordingPolicyRevision: 0,
      runtimeImageDigest: `sha256:${"c".repeat(64)}`,
    });
    const page = (records: ReturnType<typeof record>[], nextOffset?: number) => ({ records, total: records.length, offset: 0, limit: 100, nextOffset });
    let releaseLoadMore!: (value: ReturnType<typeof page>) => void;
    const listTerminalCommands = vi.fn((projectId: string, _search: string, offset: number) => {
      if (offset > 0) return new Promise<ReturnType<typeof page>>((resolve) => { releaseLoadMore = resolve; });
      return Promise.resolve(projectId === "project-2" ? page([record("command-other", "id")]) : page([record("command-first", "ls -la")], 100));
    });
    const api = {
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue({ engagementId: "project-1", enabled: true, captureMode: "selected_tools", recordCount: 3, recordedOutputCount: 0, metadataOnlyCount: 3, classificationFailureCount: 0, degradedCount: 0, truncatedCount: 0, auditGapCount: 0, capturedOutputBytes: 0 }),
      listTerminalCommands,
      terminalRecordingTools: vi.fn().mockResolvedValue({ engagementId: "project-1", inventoryStatus: "verified", runtimeImageDigest: `sha256:${"c".repeat(64)}`, manifestSha256: "d".repeat(64), defaultTools: ["nmap"], customTools: [], disabledTools: [], effectiveTools: ["nmap"], revision: 0 }),
    } as unknown as ApiClient;
    const user = userEvent.setup();
    const { rerender } = render(<DialogProvider><TerminalCommandHistoryPanel api={api} engagementId="project-1" /></DialogProvider>);

    expect(await screen.findByText("ls -la")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(listTerminalCommands).toHaveBeenLastCalledWith("project-1", "", 100, 100, undefined));

    rerender(<DialogProvider><TerminalCommandHistoryPanel api={api} engagementId="project-2" /></DialogProvider>);
    expect(await screen.findByText("id")).toBeVisible();
    expect(screen.queryByText("ls -la")).not.toBeInTheDocument();

    releaseLoadMore(page([record("command-late", "cat /etc/passwd")], 200));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(screen.queryByText("cat /etc/passwd")).not.toBeInTheDocument();
    expect(screen.getByText("id")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });
});
