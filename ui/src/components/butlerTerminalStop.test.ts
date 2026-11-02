import { describe, expect, it, vi } from "vitest";
import { mapTerminalSessionList, type TerminalSessionList } from "../api/terminalTypes";
import { applyButlerTerminalStop, type TerminalStopApi } from "./butlerTerminalStop";

const projectId = "project-one";
const sessionId = "terminal-one";
const createdAt = "2026-10-30T20:00:00+00:00";
const active: TerminalSessionList = {
  engagementId: projectId,
  sessions: [{ sessionId, createdAt, state: "running", attached: true }],
};
const empty: TerminalSessionList = { engagementId: projectId, sessions: [] };
const action = { projectId, sessionId, expectedCreatedAt: createdAt };

describe("Butler terminal stop", () => {
  it("maps only ticket-free session fields from Core", () => {
    const wire = {
      engagement_id: projectId,
      sessions: [{
        session_id: sessionId, created_at: createdAt, state: "running" as const, attached: true,
        websocket_ticket: "one-use-secret",
      }],
    };
    expect(mapTerminalSessionList(wire)).toEqual(active);
    expect(JSON.stringify(mapTerminalSessionList(wire))).not.toContain("one-use-secret");
  });

  it("refuses a different selected Project and a stale Core session before confirmation", async () => {
    const api: TerminalStopApi = {
      listProjectContainerTerminals: vi.fn().mockResolvedValue(active),
      closeProjectContainerTerminal: vi.fn().mockResolvedValue(empty),
    };
    const confirm = vi.fn().mockResolvedValue(true);
    await expect(applyButlerTerminalStop(api, action, () => "other-project", confirm)).rejects.toThrow("original Project");
    expect(api.listProjectContainerTerminals).not.toHaveBeenCalled();
    await expect(applyButlerTerminalStop(api, { ...action, expectedCreatedAt: "stale" }, () => projectId, confirm)).rejects.toThrow("changed");
    expect(confirm).not.toHaveBeenCalled();
    expect(api.closeProjectContainerTerminal).not.toHaveBeenCalled();
  });

  it("keeps a live session when confirmation is cancelled", async () => {
    const api: TerminalStopApi = {
      listProjectContainerTerminals: vi.fn().mockResolvedValue(active),
      closeProjectContainerTerminal: vi.fn().mockResolvedValue(empty),
    };
    await expect(applyButlerTerminalStop(api, action, () => projectId, async () => false))
      .resolves.toBe("Stop cancelled; Core was not asked to close this terminal.");
    expect(api.closeProjectContainerTerminal).not.toHaveBeenCalled();
  });

  it("refuses to stop after the operator switches Projects during confirmation", async () => {
    const api: TerminalStopApi = {
      listProjectContainerTerminals: vi.fn().mockResolvedValue(active),
      closeProjectContainerTerminal: vi.fn().mockResolvedValue(empty),
    };
    let selected = projectId;
    await expect(applyButlerTerminalStop(api, action, () => selected, async () => {
      selected = "other-project";
      return true;
    })).rejects.toThrow("selected Project changed");
    expect(api.closeProjectContainerTerminal).not.toHaveBeenCalled();
  });

  it("stops one exact session and reports success only after a fresh Core read", async () => {
    const api: TerminalStopApi = {
      listProjectContainerTerminals: vi.fn().mockResolvedValueOnce(active).mockResolvedValueOnce(empty),
      closeProjectContainerTerminal: vi.fn().mockResolvedValue(empty),
    };
    const confirm = vi.fn().mockResolvedValue(true);
    await expect(applyButlerTerminalStop(api, action, () => projectId, confirm))
      .resolves.toContain("Stopped terminal");
    expect(confirm).toHaveBeenCalledWith(active.sessions[0]);
    expect(api.closeProjectContainerTerminal).toHaveBeenCalledWith(projectId, sessionId, createdAt);
    expect(api.listProjectContainerTerminals).toHaveBeenCalledTimes(2);
  });

  it("retries a terminal whose earlier removal Core could not verify", async () => {
    const stopping: TerminalSessionList = {
      engagementId: projectId,
      sessions: [{...active.sessions[0], state: "stopping"}],
    };
    const api: TerminalStopApi = {
      listProjectContainerTerminals: vi.fn().mockResolvedValueOnce(stopping).mockResolvedValueOnce(empty),
      closeProjectContainerTerminal: vi.fn().mockResolvedValue(empty),
    };
    const confirm = vi.fn().mockResolvedValue(true);
    await expect(applyButlerTerminalStop(api, action, () => projectId, confirm))
      .resolves.toContain("Stopped terminal");
    expect(confirm).toHaveBeenCalledWith(stopping.sessions[0]);
    expect(api.closeProjectContainerTerminal).toHaveBeenCalledWith(projectId, sessionId, createdAt);
  });

  it("does not claim success after an uncertain stop or a stale readback", async () => {
    const api: TerminalStopApi = {
      listProjectContainerTerminals: vi.fn().mockResolvedValue(active),
      closeProjectContainerTerminal: vi.fn().mockRejectedValue(new Error("connection lost")),
    };
    await expect(applyButlerTerminalStop(api, action, () => projectId, async () => true)).rejects.toThrow("Inspect Terminal");
    expect(api.listProjectContainerTerminals).toHaveBeenCalledTimes(1);
    api.closeProjectContainerTerminal = vi.fn().mockResolvedValue(empty);
    await expect(applyButlerTerminalStop(api, action, () => projectId, async () => true)).rejects.toThrow("readback");
    api.listProjectContainerTerminals = vi.fn().mockResolvedValueOnce(active).mockRejectedValueOnce(new Error("connection lost"));
    await expect(applyButlerTerminalStop(api, action, () => projectId, async () => true)).rejects.toThrow("readback failed");
  });
});
