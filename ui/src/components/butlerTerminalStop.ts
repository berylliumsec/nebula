/** Apply one Butler terminal stop on the requesting browser after Core readback. */

import type { TerminalSessionList, TerminalSessionSummary } from "../api/terminalTypes";

export interface TerminalStopApi {
  listProjectContainerTerminals(projectId: string): Promise<TerminalSessionList>;
  closeProjectContainerTerminal(projectId: string, sessionId: string, expectedCreatedAt: string): Promise<TerminalSessionList>;
}

export interface TerminalStopAction {
  projectId: string;
  sessionId: string;
  expectedCreatedAt: string;
}

export async function applyButlerTerminalStop(
  api: TerminalStopApi,
  action: TerminalStopAction,
  selectedProjectId: () => string | undefined,
  confirm: (session: TerminalSessionSummary) => Promise<boolean>,
): Promise<string> {
  const stillSelected = () => selectedProjectId() === action.projectId;
  if (!stillSelected()) throw new Error("Select the original Project before stopping its terminal.");
  const current = await api.listProjectContainerTerminals(action.projectId);
  if (!stillSelected() || current.engagementId !== action.projectId) {
    throw new Error("The selected Project changed. Ask Butler to list terminals again.");
  }
  const session = current.sessions.find((item) => item.sessionId === action.sessionId);
  if (!session || session.createdAt !== action.expectedCreatedAt) {
    throw new Error("This terminal changed or stopped. Ask Butler to list terminals again.");
  }
  if (!(await confirm(session))) return "Stop cancelled; Core was not asked to close this terminal.";
  if (!stillSelected()) throw new Error("The selected Project changed. Ask Butler to list terminals again.");

  let saved: TerminalSessionList;
  try {
    saved = await api.closeProjectContainerTerminal(action.projectId, action.sessionId, action.expectedCreatedAt);
  } catch (reason) {
    throw new Error("Core did not confirm the terminal stop. Inspect Terminal before retrying; it may already be closed.", {cause: reason});
  }
  if (saved.engagementId !== action.projectId || saved.sessions.some((item) => item.sessionId === action.sessionId)) {
    throw new Error("Core still reports the terminal as active. Inspect Terminal before retrying.");
  }
  let observed: TerminalSessionList;
  try {
    observed = await api.listProjectContainerTerminals(action.projectId);
  } catch (reason) {
    throw new Error("Core accepted the stop, but its readback failed. Inspect Terminal before retrying; it may already be closed.", {cause: reason});
  }
  if (!stillSelected() || observed.engagementId !== action.projectId || observed.sessions.some((item) => item.sessionId === action.sessionId)) {
    throw new Error("Core did not confirm the terminal stop after readback. Inspect Terminal before retrying.");
  }
  return `Stopped terminal ${action.sessionId.slice(0, 8)} in this Project.`;
}
