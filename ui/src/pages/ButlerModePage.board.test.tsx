import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { ButlerModePage } from "./ButlerModePage";

const requestIfChanged = vi.fn();
const api = { requestIfChanged };
vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({ api, coreState: "online", engagement: undefined, engagements: [], providers: [], harnesses: [], selectEngagement: vi.fn() }),
}));
vi.mock("../state/ButlerPanelBridge", () => ({ useButlerPanelBridge: () => null }));
vi.mock("../components/DialogSystem", () => ({ useConfirmation: () => vi.fn() }));
vi.mock("../hooks/useCompactLayout", () => ({ useCompactLayout: () => false }));
vi.mock("../components/NebulaVoice", () => ({ MicrophoneIcon: () => null }));
vi.mock("../components/ButlerOversight", () => ({ ButlerOversight: () => null }));
vi.mock("../components/ButlerMemory", () => ({ ButlerMemory: () => null }));
vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

const research = { id: "p1", name: "Research" };
const bounty = { id: "p2", name: "Bounty" };

function wire(title: string, project: { id: string; name: string }, section: string, state: string, goalStatus: string, now?: string) {
  return {
    session_id: title, title, project, section, state, backend: "provider", model: "model", is_subagent: false,
    updated_at: "2026-11-20T10:00:00Z", now: now ?? null,
    goal: { objective: "Goal", status: goalStatus, current_step: 0, plan_length: 2, revision: 1 },
  };
}

const poll = {
  counts: { active: 2, idle: 1, completed: 2 },
  states: { waiting: 1, working: 1, failed: 0 },
  projects: [
    { ...bounty, counts: { active: 1, idle: 0, completed: 0 } },
    { ...research, counts: { active: 1, idle: 1, completed: 2 } },
  ],
  items: [
    wire("Install driver", bounty, "active", "waiting", "running", "Approve run_command: make install"),
    wire("Fuzz parser", research, "active", "working", "running", "Working"),
    wire("Paused triage", research, "idle", "idle", "paused", "Paused"),
  ],
  next_offset: null,
  truncated: false,
};
const completedPage = {
  ...poll,
  states: null,
  items: [
    wire("Old sweep", research, "completed", "idle", "completed", "Completed"),
    wire("Dropped idea", research, "completed", "idle", "cancelled", "Cancelled"),
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  requestIfChanged.mockImplementation(async (path: string) => {
    if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
    if (path.includes("section=completed")) return { value: completedPage, etag: '"c"' };
    return { value: poll, etag: '"b"' };
  });
});

const mount = () => render(<MemoryRouter initialEntries={["/butler"]}><ButlerModePage /></MemoryRouter>);

it("groups goal chats by project into Active, Idle and a collapsed Completed", async () => {
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  const working = await within(board).findByRole("article", { name: "Fuzz parser: Working, Active" });
  expect(working.querySelector("[data-section]")).toHaveAttribute("data-section", "active");
  const paused = within(board).getByRole("article", { name: "Paused triage: Paused, Idle" });
  expect(paused.querySelector("[data-section]")).toHaveAttribute("data-section", "idle");
  expect(within(paused).getByText("Paused")).toBeVisible();
  // "Needs you" stands out within Active.
  const waiting = within(board).getByRole("article", { name: "Install driver: Needs you, Active" });
  expect(within(waiting).getByText("Approve run_command: make install")).toHaveAttribute("data-state", "waiting");

  // Projects needing the operator lead; each header counts goals by section.
  const heads = within(board).getAllByRole("button", { name: /goals?/ });
  expect(heads.map((head) => head.textContent)).toEqual(["Bounty1 goal1 active", "Research4 goals1 active1 idle2 completed"]);

  const projectGroup = board.querySelector('[data-project="p1"]') as HTMLElement;
  const completed = within(projectGroup).getByRole("button", { name: /^Completed/ });
  expect(completed).toHaveAttribute("aria-expanded", "false");
  expect(within(projectGroup).getByRole("button", { name: /^Active/ })).toHaveAttribute("aria-expanded", "true");
  expect(within(board).queryByRole("article", { name: /Old sweep/ })).not.toBeInTheDocument();
  expect(requestIfChanged.mock.calls.some(([path]) => String(path).includes("section=completed"))).toBe(false);

  // Opening Completed reads that project's page and this device remembers it.
  await userEvent.click(completed);
  const done = await within(board).findByRole("article", { name: "Old sweep: Completed, Completed" });
  expect(requestIfChanged).toHaveBeenCalledWith("butler/board?section=completed&project_id=p1&limit=50", undefined, { signal: undefined });
  const dropped = within(board).getByRole("article", { name: "Dropped idea: Cancelled, Completed" });
  expect(done.querySelector("[data-section]")).toHaveAttribute("data-goal", "completed");
  expect(dropped.querySelector("[data-section]")).toHaveAttribute("data-goal", "cancelled");
  expect(JSON.parse(localStorage.getItem("nebula.butler-mode.sections") ?? "{}")).toEqual({ "p1:completed": true });

  // Collapsing Active hides its cards, and the choice is remembered too.
  await userEvent.click(within(projectGroup).getByRole("button", { name: /^Active/ }));
  await waitFor(() => expect(within(board).queryByRole("article", { name: /Fuzz parser/ })).not.toBeInTheDocument());
  expect(JSON.parse(localStorage.getItem("nebula.butler-mode.sections") ?? "{}")).toEqual({ "p1:completed": true, "p1:active": false });
});

it("groups by status into the three sections across projects", async () => {
  localStorage.setItem("nebula.butler-mode.grouping", "attention");
  localStorage.setItem("nebula.butler-mode.sections", "{broken");
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  await within(board).findByRole("article", { name: "Fuzz parser: Working, Active" });
  expect(within(board).getByRole("button", { name: /Group: Status/ })).toBeVisible();
  const sections = [...board.querySelectorAll<HTMLElement>("div[data-section] > button")].map((head) => [head.parentElement?.dataset.section, head.getAttribute("aria-expanded")]);
  expect(sections).toEqual([["active", "true"], ["idle", "true"], ["completed", "false"]]);
  // Cards name their project when the board is not grouped by project.
  expect(within(within(board).getByRole("article", { name: "Install driver: Needs you, Active" })).getByText(/Bounty/)).toBeVisible();
});
