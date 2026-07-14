import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { DialogProvider } from "./DialogSystem";
import { TerminalCommandHistoryPanel } from "./TerminalCommandHistoryPanel";

describe("TerminalCommandHistoryPanel", () => {
  it("loads searchable command metadata and supports disable and confirmed clear", async () => {
    const status = {
      engagementId: "project-1",
      enabled: true,
      recordCount: 1,
      retentionDays: 90,
      maxRecords: 10_000,
    };
    const api = {
      terminalCommandHistoryStatus: vi.fn().mockResolvedValue(status),
      listTerminalCommands: vi.fn().mockResolvedValue({
        records: [{
          id: "command-1",
          engagementId: "project-1",
          sessionId: "terminal-1",
          command: "nmap -sV 10.0.0.8",
          cwd: "/workspace",
          exitCode: 0,
          occurredAt: "2026-07-13T20:00:00Z",
        }],
        total: 1,
        offset: 0,
        limit: 100,
      }),
      setTerminalCommandHistoryEnabled: vi.fn().mockResolvedValue({ ...status, enabled: false }),
      clearTerminalCommands: vi.fn().mockResolvedValue(1),
    } as unknown as ApiClient;
    const user = userEvent.setup();
    render(<DialogProvider><TerminalCommandHistoryPanel api={api} engagementId="project-1" /></DialogProvider>);

    expect(await screen.findByText("nmap -sV 10.0.0.8")).toBeVisible();
    expect(screen.getByText("/workspace")).toBeVisible();
    expect(screen.getByText("exit 0")).toBeVisible();
    expect(screen.queryByText(/terminal output/i)).not.toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Search terminal commands" }), "nmap");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.listTerminalCommands).toHaveBeenLastCalledWith("project-1", "nmap", 0, 100, expect.any(AbortSignal)));

    await user.click(screen.getByRole("checkbox", { name: "Record commands" }));
    expect(api.setTerminalCommandHistoryEnabled).toHaveBeenCalledWith("project-1", false);
    expect(await screen.findByText(/New commands are not being recorded/)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(await screen.findByRole("heading", { name: "Clear local command history?" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear history" }));
    await waitFor(() => expect(api.clearTerminalCommands).toHaveBeenCalledWith("project-1"));
    expect(screen.getByText("No matching commands")).toBeVisible();
  });
});
