import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ButlerModePage } from "./ButlerModePage";

const requestIfChanged = vi.fn();
const writeChatGoal = vi.fn();
const getChatGoal = vi.fn();
const confirm = vi.fn();
const listChatMessages = vi.fn(async () => []);
const getPendingChatTurn = vi.fn(async () => null);
const holdChats = vi.fn();
const releaseChats = vi.fn();
const setChatSessionPriority = vi.fn();
const request = vi.fn();
const api = { request, requestIfChanged, writeChatGoal, getChatGoal, listChatMessages, getPendingChatTurn, setChatSessionPriority, projectHolds: { holdChats, releaseChats } };
vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({ api, coreState: "online", engagement: undefined, engagements: [], providers: [], harnesses: [], selectEngagement: vi.fn() }),
}));
vi.mock("../state/ButlerPanelBridge", () => ({ useButlerPanelBridge: () => null }));
vi.mock("../components/DialogSystem", () => ({ useConfirmation: () => confirm }));
const layout = vi.hoisted(() => ({ compact: false, shell: false }));
vi.mock("../hooks/useCompactLayout", () => ({ useCompactLayout: () => layout.compact, isNebulaShell: () => layout.shell }));
vi.mock("../components/NebulaVoice", () => ({ MicrophoneIcon: () => null }));
vi.mock("../components/ButlerOversight", () => ({ ButlerOversight: () => null }));
vi.mock("../components/ButlerMemory", () => ({ ButlerMemory: () => null }));
vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));
vi.mock("./ButlerChatBrief", () => ({ ButlerChatBrief: ({ onAsk, sessionId, butlerCase }: { onAsk: (value: unknown) => void; sessionId: string; butlerCase?: { stage: { label: string }; onOpen?: () => void } }) => <>
  <button type="button" onClick={() => onAsk(undefined)}>Ask Butler about this</button>
  {butlerCase && <p>{`Brief ${sessionId} · Butler · ${butlerCase.stage.label}${butlerCase.onOpen ? " · opens check" : ""}`}</p>}
</> }));

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
  layout.compact = false;
  layout.shell = false;
  localStorage.clear();
  requestIfChanged.mockImplementation(async (path: string) => {
    if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
    if (path.includes("section=completed")) return { value: completedPage, etag: '"c"' };
    return { value: poll, etag: '"b"' };
  });
});

const mount = () => render(<MemoryRouter initialEntries={["/butler"]}><ButlerModePage /></MemoryRouter>);

it("shows a goal a model refusal paused with its own badge and count", async () => {
  const refused = wire("Refused triage", research, "idle", "idle", "paused", "Model refusal (2): needs operator review");
  const reason = "Model refusal (category-a) on Model X in turn t1: needs operator review";
  requestIfChanged.mockImplementation(async (path: string) => {
    if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
    return { value: { ...poll, items: [...poll.items, { ...refused, goal: { ...refused.goal, model_refusal: { paused: true, count: 2, reason } } }] }, etag: '"r"' };
  });
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  const card = await within(board).findByRole("article", { name: "Refused triage: Model refusal, Idle" });
  const badge = within(card).getByText("Model refusal · 2");
  expect(badge).toHaveAttribute("data-paused", "true");
  expect(badge).toHaveAttribute("title", reason);
  expect(within(card).getByText("Model refusal (2): needs operator review")).toBeVisible();
  // Other paused goals carry no refusal badge.
  const paused = within(board).getByRole("article", { name: "Paused triage: Paused, Idle" });
  expect(within(paused).queryByText(/Model refusal/)).toBeNull();
});

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
  const sections = [...board.querySelectorAll<HTMLElement>("div[data-section] > div > button")].map((head) => [head.parentElement?.parentElement?.dataset.section, head.getAttribute("aria-expanded")]);
  expect(sections).toEqual([["active", "true"], ["idle", "true"], ["completed", "false"]]);
  // Cards name their project when the board is not grouped by project.
  expect(within(within(board).getByRole("article", { name: "Install driver: Needs you, Active" })).getByText(/Bounty/)).toBeVisible();
});

const idlePage = {
  ...poll,
  states: null,
  items: [
    wire("Paused triage", research, "idle", "idle", "paused", "Paused"),
    wire("Blocked build", bounty, "idle", "idle", "blocked", "Blocked"),
    wire("Proposed sweep", research, "idle", "idle", "draft", "Proposed"),
  ],
};

it("resumes the idle goals the operator ticks and keeps Core's refusal on the card", async () => {
  writeChatGoal.mockResolvedValue({});
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  const paused = await within(board).findByRole("article", { name: "Paused triage: Paused, Idle" });
  // Active cards are ticked to hold them; paused and blocked ones to resume (or hold).
  expect(within(within(board).getByRole("article", { name: "Fuzz parser: Working, Active" })).getByRole("checkbox", { name: "Select Fuzz parser to hold" })).toBeVisible();
  await userEvent.click(within(paused).getByRole("checkbox", { name: "Select Paused triage to resume" }));
  const bar = within(board).getByRole("region", { name: "Selected goal chats" });
  expect(within(bar).getByText("1 selected")).toBeVisible();
  expect(paused).toHaveAttribute("data-selected", "true");

  await userEvent.click(within(bar).getByRole("button", { name: "Resume 1" }));
  await within(board).findByText("Resumed 1 goal.");
  expect(writeChatGoal).toHaveBeenCalledWith("Paused triage", { expectedRevision: 1, action: "resume" });
  expect(within(board).queryByRole("region", { name: "Selected goal chats" })).not.toBeInTheDocument();

  writeChatGoal.mockRejectedValueOnce(Object.assign(new Error("goal budget is spent"), { status: 409 }));
  await userEvent.click(within(paused).getByRole("checkbox", { name: "Select Paused triage to resume" }));
  await userEvent.click(within(board).getByRole("button", { name: "Resume 1" }));
  await within(board).findByText(/Resumed 0 of 1\. 1 stayed idle; its card says why\./);
  expect(within(paused).getByText("Not resumed: goal budget is spent")).toHaveAttribute("data-failed", "true");
  // A refused goal stays ticked so the operator can retry it.
  expect(paused).toHaveAttribute("data-selected", "true");
  await userEvent.keyboard("{Escape}");
  expect(within(board).queryByRole("region", { name: "Selected goal chats" })).not.toBeInTheDocument();
});

it("Resume all idle reads Core's full idle list, confirms, and skips proposed goals", async () => {
  requestIfChanged.mockImplementation(async (path: string) => {
    if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
    if (path.includes("section=idle")) return { value: idlePage, etag: '"i"' };
    return { value: poll, etag: '"b"' };
  });
  writeChatGoal.mockResolvedValue({});
  confirm.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  await within(board).findByRole("article", { name: "Paused triage: Paused, Idle" });

  await userEvent.click(within(board).getByRole("button", { name: "Resume all idle" }));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  expect(requestIfChanged).toHaveBeenCalledWith("butler/board?section=idle&limit=200", undefined, { signal: undefined });
  expect(confirm.mock.calls[0][0]).toMatchObject({ title: "Resume 2 idle goals?", confirmLabel: "Resume 2" });
  expect(confirm.mock.calls[0][0].message).toContain("in 2 projects");
  expect(writeChatGoal).not.toHaveBeenCalled();

  await userEvent.click(within(board).getByRole("button", { name: "Resume all idle" }));
  await within(board).findByText("Resumed 2 goals.");
  expect(writeChatGoal.mock.calls.map(([id]) => id).sort()).toEqual(["Blocked build", "Paused triage"]);
});

it("Select all in a project ticks every resumable idle goal Core lists there", async () => {
  requestIfChanged.mockImplementation(async (path: string) => {
    if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
    if (path.includes("section=idle")) return { value: { ...idlePage, items: idlePage.items.filter((item) => item.project.id === "p1") }, etag: '"i"' };
    return { value: poll, etag: '"b"' };
  });
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  await within(board).findByRole("article", { name: "Paused triage: Paused, Idle" });
  const selectAll = within(board).getByRole("checkbox", { name: "Select all paused or blocked goals in Research" });
  await userEvent.click(selectAll);
  await waitFor(() => expect(selectAll).toBeChecked());
  expect(requestIfChanged).toHaveBeenCalledWith("butler/board?section=idle&project_id=p1&limit=200", undefined, { signal: undefined });
  expect(within(within(board).getByRole("region", { name: "Selected goal chats" })).getByText("1 selected")).toBeVisible();
  await userEvent.click(selectAll);
  expect(within(board).queryByRole("region", { name: "Selected goal chats" })).not.toBeInTheDocument();
});

describe("asking about a chat while the open thread is answering", () => {
  const thread = (state: string) => ({ session_id: "t1", title: "Earlier question", project: bounty, state, updated_at: "2026-11-20T10:00:00Z" });
  const serveThreads = (states: string[]) => {
    let reads = 0;
    requestIfChanged.mockImplementation(async (path: string) => {
      if (path.startsWith("butler/threads")) { const state = states[Math.min(reads, states.length - 1)]; reads += 1; return { value: { items: [thread(state)] }, etag: `"t${reads}"` }; }
      return { value: poll, etag: '"b"' };
    });
  };
  const mountOnThread = () => render(<MemoryRouter initialEntries={["/butler?thread=t1&chat=Install%20driver"]}><ButlerModePage /></MemoryRouter>);

  it("starts as a new thread, then quotes into the thread once it settles", async () => {
    serveThreads(["working", "idle"]);
    mountOnThread();
    const reply = await screen.findByRole("button", { name: "Reply in this thread" });
    await userEvent.click(await screen.findByRole("button", { name: "Ask Butler about this" }));
    expect(screen.getByRole("button", { name: "New thread" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Install driver", { selector: "span" })).toBeVisible();
    await waitFor(() => expect(reply).toHaveAttribute("aria-pressed", "true"), { timeout: 8000 });
    expect(screen.getByRole("button", { name: "New thread" })).toHaveAttribute("aria-pressed", "false");
  }, 15_000);

  it("keeps the operator's own choice of a new thread", async () => {
    serveThreads(["working", "idle"]);
    mountOnThread();
    const reply = await screen.findByRole("button", { name: "Reply in this thread" });
    await userEvent.click(await screen.findByRole("button", { name: "Ask Butler about this" }));
    await userEvent.click(screen.getByRole("button", { name: "New thread" }));
    await waitFor(() => expect(requestIfChanged.mock.calls.filter(([path]) => String(path).startsWith("butler/threads")).length).toBeGreaterThan(1), { timeout: 8000 });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(reply).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "New thread" })).toHaveAttribute("aria-pressed", "true");
  }, 15_000);
});

describe("the All projects link", () => {
  const mountWithProjects = () => render(<MemoryRouter initialEntries={["/butler"]}><Routes>
    <Route path="/butler" element={<ButlerModePage />} />
    <Route path="/projects" element={<h1>Projects page</h1>} />
  </Routes></MemoryRouter>);

  it("opens All projects from the top bar", async () => {
    mountWithProjects();
    const link = await screen.findByRole("link", { name: "All projects" });
    expect(link).toHaveAttribute("href", "/projects");
    await userEvent.click(link);
    expect(await screen.findByRole("heading", { name: "Projects page" })).toBeVisible();
  });

  it("opens All projects from More on a phone", async () => {
    layout.compact = true;
    mountWithProjects();
    expect(screen.queryByRole("link", { name: "All projects" })).toBeNull();
    await userEvent.click(await screen.findByRole("tab", { name: /More/ }));
    const sheet = screen.getByRole("dialog", { name: "More in Butler" });
    await userEvent.click(within(sheet).getByRole("link", { name: "All projects" }));
    expect(await screen.findByRole("heading", { name: "Projects page" })).toBeVisible();
  });

  it("in the iPhone app, has nothing to exit to and still opens All projects", async () => {
    layout.compact = true;
    layout.shell = true;
    try {
      mountWithProjects();
      await screen.findByRole("tab", { name: /More/ });
      expect(screen.queryByRole("button", { name: "Exit Butler mode" })).toBeNull();
      await userEvent.keyboard("{Escape}");
      expect(screen.getByRole("heading", { name: "Butler", level: 1 })).toBeVisible();
      await userEvent.click(screen.getByRole("tab", { name: /More/ }));
      const sheet = screen.getByRole("dialog", { name: "More in Butler" });
      expect(within(sheet).queryByRole("button", { name: "Open full Nebula" })).toBeNull();
      await userEvent.click(within(sheet).getByRole("link", { name: "All projects" }));
      expect(await screen.findByRole("heading", { name: "Projects page" })).toBeVisible();
    } finally {
      layout.shell = false;
    }
  });
});


it("holds ticked chats at once, with no confirmation, and Undo releases the batch", async () => {
  holdChats.mockResolvedValue({ batchId: "batch-1", sessionIds: ["Fuzz parser"] });
  releaseChats.mockResolvedValue({});
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  const fuzz = await within(board).findByRole("article", { name: "Fuzz parser: Working, Active" });
  await userEvent.click(within(fuzz).getByRole("checkbox", { name: "Select Fuzz parser to hold" }));
  const bar = within(board).getByRole("region", { name: "Selected goal chats" });
  // Only a resumable idle selection offers Resume; this one offers Hold.
  expect(within(bar).queryByRole("button", { name: /^Resume/ })).not.toBeInTheDocument();

  await userEvent.click(within(bar).getByRole("button", { name: "Hold 1" }));
  expect(confirm).not.toHaveBeenCalled();
  expect(holdChats).toHaveBeenCalledWith("p1", { sessionIds: ["Fuzz parser"], allActive: undefined });
  const toast = await within(board).findByRole("status", { name: "Held chats" });
  expect(within(toast).getByText("Held 1 chat")).toBeVisible();

  await userEvent.click(within(toast).getByRole("button", { name: "Undo" }));
  await within(board).findByText(/Released 1 chat/);
  expect(releaseChats).toHaveBeenCalledWith("p1", expect.objectContaining({ batchId: "batch-1" }));
  expect(within(board).queryByRole("status", { name: "Held chats" })).not.toBeInTheDocument();
});

it("Hold all active holds every busy project's running chats without asking", async () => {
  holdChats.mockImplementation(async (projectId: string) => ({ batchId: `b-${projectId}`, sessionIds: [projectId === "p1" ? "Fuzz parser" : "Install driver"] }));
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  await within(board).findByRole("article", { name: "Fuzz parser: Working, Active" });
  await userEvent.click(within(board).getByRole("button", { name: "Hold all active" }));
  await within(board).findByText("Held 2 chats");
  expect(confirm).not.toHaveBeenCalled();
  expect(holdChats.mock.calls.map(([id, choice]) => [id, choice.allActive]).sort()).toEqual([["p1", true], ["p2", true]]);
});

it("shows held chats under Held with their reason, and releases one or all", async () => {
  releaseChats.mockResolvedValue({});
  const hold = { id: "h1", batch_id: "batch-9", reason: "Priorities changed", created_at: "2026-11-20T09:00:00Z" };
  requestIfChanged.mockImplementation(async (path: string) => {
    if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
    return { value: { ...poll, items: [
      ...poll.items.slice(0, 2),
      { ...wire("Old research", research, "idle", "idle", "paused", "Paused"), hold },
    ] }, etag: '"h"' };
  });
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  const held = await within(board).findByRole("article", { name: "Old research: Held, Held" });
  expect(held).toHaveAttribute("data-section", "held");
  expect(within(held).getByText("On hold · Priorities changed")).toBeVisible();
  // A held chat is neither resumable nor holdable again from the board.
  expect(within(held).queryByRole("checkbox")).not.toBeInTheDocument();
  const projectGroup = board.querySelector('[data-project="p1"]') as HTMLElement;
  expect(within(projectGroup).getByRole("button", { name: /^Held.*Priorities changed/ })).toHaveAttribute("aria-expanded", "true");
  // Core counted it as idle; the board shows it under Held instead.
  expect(within(projectGroup).queryByRole("button", { name: /^Idle/ })).not.toBeInTheDocument();

  await userEvent.click(within(held).getByRole("button", { name: "Release Old research" }));
  await within(board).findByText(/Released 1 chat/);
  expect(releaseChats).toHaveBeenLastCalledWith("p1", expect.objectContaining({ batchId: "batch-9" }));
  await userEvent.click(within(projectGroup).getByRole("button", { name: "Release all" }));
  await waitFor(() => expect(releaseChats).toHaveBeenCalledTimes(2));
});

it("lists high-priority chats first with a chevron and toggles priority from the card", async () => {
  setChatSessionPriority.mockResolvedValue({});
  requestIfChanged.mockImplementation(async (path: string) => {
    if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
    return { value: { ...poll, items: [
      wire("Fuzz parser", research, "active", "working", "running", "Working"),
      { ...wire("New direction", research, "active", "working", "running", "Working"), priority: "high", session_revision: 4 },
    ] }, etag: '"p"' };
  });
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  const urgent = await within(board).findByRole("article", { name: "New direction: Working, Active, high priority" });
  const cards = within(board.querySelector('[data-project="p1"]') as HTMLElement).getAllByRole("article");
  expect(cards[0]).toBe(urgent);
  expect(urgent).toHaveAttribute("data-priority", "high");
});

it("shows Butler's case stage on a goal card and offers its Butler check", async () => {
  const withCase = {
    ...poll,
    items: [
      { ...wire("Fuzz parser", research, "active", "working", "running", "Working"), butler_case: { id: "case-1", status: "needs_attention", session_id: "case-chat", updated_at: "2026-11-20T10:02:00Z" },
        work_stage: { key: "simulation", label: "Simulation", item_id: "item-1", item_title: "Fuzz parser", item_status: "in_progress" } },
      wire("Paused triage", research, "idle", "idle", "paused", "Paused"),
    ],
  };
  requestIfChanged.mockImplementation(async (path: string) => {
    if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
    return { value: withCase, etag: '"b"' };
  });
  mount();
  const board = screen.getByRole("region", { name: "Status board" });
  const card = await within(board).findByRole("article", { name: "Fuzz parser: Working, Active, Work stage Simulation, in progress, Butler needs you" });
  // The project Work stage leads; Butler's status follows as its own quieter line.
  const workStage = card.querySelector(".chat-work-stage");
  expect(workStage).toHaveAttribute("data-state", "active");
  expect(workStage).toHaveTextContent("Simulation");
  expect(workStage?.compareDocumentPosition(card.querySelector(".butler-stage")!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  const stage = within(card).getByText("Butler · Needs you").closest(".butler-stage");
  expect(stage).toHaveAttribute("data-stage", "needs-you");
  expect(stage).toHaveAttribute("title", expect.stringMatching(/needs your decision/));
  // A card without a case shows no Butler stage.
  const quiet = within(board).getByRole("article", { name: "Paused triage: Paused, Idle" });
  expect(quiet.querySelector(".butler-stage")).toBeNull();
  expect(quiet.querySelector(".chat-work-stage")).toBeNull();
  // Opening the card's brief passes the case so the brief can open the Butler check.
  await userEvent.click(within(card).getByRole("button", { expanded: false }));
  expect(await screen.findByText("Brief Fuzz parser · Butler · Needs you · opens check")).toBeInTheDocument();
});

describe("stage filter", () => {
  const stagedPoll = {
    ...poll,
    stages: [{ label: "Recon", counts: { active: 1, idle: 0, completed: 0 } }, { label: "Validation", counts: { active: 1, idle: 1, completed: 0 } }],
    no_stage: { active: 0, idle: 0, completed: 2 },
  };
  const reconPoll = {
    ...stagedPoll,
    counts: { active: 1, idle: 0, completed: 0 },
    projects: [{ ...research, counts: { active: 1, idle: 0, completed: 0 } }],
    items: [{ ...wire("Fuzz parser", research, "active", "working", "running", "Working"), work_stage: { key: "recon", label: "Recon", item_id: "w1", item_title: "Recon", item_status: "in_progress" } }],
  };
  const serve = (path: string) => {
    if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
    if (path.includes("stage=Recon") && path.includes("section=idle")) return { value: { ...reconPoll, items: [] }, etag: '"ri"' };
    if (path.includes("stage=Recon")) return { value: reconPoll, etag: '"r"' };
    return { value: stagedPoll, etag: '"b"' };
  };

  it("narrows the board to a chosen stage with counts, remembers it, and clears it", async () => {
    requestIfChanged.mockImplementation(async (path: string) => serve(path));
    const view = mount();
    const board = screen.getByRole("region", { name: "Status board" });
    await within(board).findByRole("article", { name: "Install driver: Needs you, Active" });

    await userEvent.click(within(board).getByRole("button", { name: "Filter by stage: All" }));
    const picker = within(board).getByRole("dialog", { name: "Filter by stage" });
    const options = within(picker).getAllByRole("checkbox");
    expect(options.map((option) => option.closest("label")?.textContent)).toEqual(["Recon1", "Validation2", "No stage2"]);
    await userEvent.click(within(picker).getByRole("checkbox", { name: /Recon/ }));

    await waitFor(() => expect(requestIfChanged).toHaveBeenCalledWith("butler/board?stage=Recon", undefined, expect.anything()));
    await within(board).findByText(/Showing stage Recon · 1 goal chat\. Hold and Resume act only on these\./);
    await waitFor(() => expect(within(board).queryByRole("article", { name: "Install driver: Needs you, Active" })).not.toBeInTheDocument());
    expect(within(board).getByRole("article", { name: /^Fuzz parser: Working, Active, / })).toBeInTheDocument();
    expect(within(board).getByRole("button", { name: "Hold active in Recon" })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("nebula.butler-mode.stage-filter")!)).toEqual({ labels: ["Recon"], noStage: false });

    // A reload restores the remembered stage.
    view.unmount();
    requestIfChanged.mockClear();
    mount();
    const again = screen.getByRole("region", { name: "Status board" });
    await within(again).findByRole("button", { name: "Filter by stage: Recon" });
    await waitFor(() => expect(requestIfChanged).toHaveBeenCalledWith("butler/board?stage=Recon", undefined, expect.anything()));

    await userEvent.click(within(again).getByRole("button", { name: "Clear stage filter" }));
    await within(again).findByRole("button", { name: "Filter by stage: All" });
    await within(again).findByRole("article", { name: "Install driver: Needs you, Active" });
    expect(within(again).getByRole("button", { name: "Hold all active" })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("nebula.butler-mode.stage-filter")!)).toEqual({ labels: [], noStage: false });
  });

  it("holds only the filtered running chats, by name", async () => {
    localStorage.setItem("nebula.butler-mode.stage-filter", JSON.stringify({ labels: ["Recon"], noStage: false }));
    requestIfChanged.mockImplementation(async (path: string) => serve(path));
    holdChats.mockResolvedValue({ batchId: "b1", sessionIds: ["Fuzz parser"] });
    mount();
    const board = screen.getByRole("region", { name: "Status board" });
    await within(board).findByRole("article", { name: /^Fuzz parser: Working, Active, / });

    await userEvent.click(within(board).getByRole("button", { name: "Hold active in Recon" }));
    await within(board).findByText("Held 1 chat");
    expect(holdChats).toHaveBeenCalledTimes(1);
    expect(holdChats).toHaveBeenCalledWith("p1", { sessionIds: ["Fuzz parser"], allActive: undefined });
  });

  it("Resume idle reads only the filtered stage's idle goals and says so", async () => {
    localStorage.setItem("nebula.butler-mode.stage-filter", JSON.stringify({ labels: ["Recon"], noStage: false }));
    const withIdle = { ...reconPoll, counts: { active: 1, idle: 1, completed: 0 } };
    const idleRecon = { ...withIdle, items: [{ ...wire("Paused recon", research, "idle", "idle", "paused", "Paused") }] };
    requestIfChanged.mockImplementation(async (path: string) => {
      if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
      if (path.includes("section=idle")) return { value: idleRecon, etag: '"i"' };
      return { value: withIdle, etag: '"r"' };
    });
    confirm.mockResolvedValue(false);
    mount();
    const board = screen.getByRole("region", { name: "Status board" });
    await userEvent.click(await within(board).findByRole("button", { name: "Resume idle in Recon" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(requestIfChanged).toHaveBeenCalledWith("butler/board?section=idle&limit=200&stage=Recon", undefined, { signal: undefined });
    expect(confirm.mock.calls[0][0].message).toContain("in stage Recon");
    expect(confirm.mock.calls[0][0].message).toContain("Goals in other stages are not resumed.");
    expect(writeChatGoal).not.toHaveBeenCalled();
  });

  it("says when no goal chat is in the chosen stage and offers every stage again", async () => {
    localStorage.setItem("nebula.butler-mode.stage-filter", JSON.stringify({ labels: ["Shipping"], noStage: false }));
    const empty = { ...stagedPoll, counts: { active: 0, idle: 0, completed: 0 }, projects: [], items: [] };
    requestIfChanged.mockImplementation(async (path: string) => path.startsWith("butler/threads") ? { value: { items: [] }, etag: '"t"' } : { value: empty, etag: '"e"' });
    mount();
    const board = screen.getByRole("region", { name: "Status board" });
    await within(board).findByText("No goal chats in stage Shipping.");
    await userEvent.click(within(board).getByRole("button", { name: "Filter by stage: Shipping" }));
    const picker = within(board).getByRole("dialog", { name: "Filter by stage" });
    expect(within(picker).getByRole("checkbox", { name: /Shipping/ })).toBeChecked();
    await userEvent.click(within(picker).getByRole("button", { name: "Show all stages" }));
    await within(board).findByRole("button", { name: "Filter by stage: All" });
  });
});

describe("operator action requests", () => {
  const needsYou = {
    id: "w1", project: research, title: "Needs you: Restart host on lab-host-1", typed: true,
    action: "restart_host", action_label: "Restart host", device_or_host: "lab-host-1", reason: "The host stopped answering after an update.",
    urgency: "blocking", status: "open", goal: { session_id: "Fuzz parser", title: "Fuzz parser", status: "blocked" }, evidence: [],
    check: { kind: "tcp_reachable", params: { port: 22 } }, last_check: { passed: false, detail: "connection refused", checked_at: "2026-11-20T10:05:00Z" },
    created_at: "2026-11-20T10:00:00Z",
  };
  const legacy = { ...needsYou, id: "w2", typed: false, title: "Needs you: plug the probe back in", action: "other", action_label: "Operator action", device_or_host: "", reason: "The bench probe is unplugged.", urgency: "low", goal: null, check: null, last_check: null };

  let open: Array<typeof needsYou | typeof legacy> = [];
  beforeEach(() => {
    open = [legacy, needsYou];
    requestIfChanged.mockImplementation(async (path: string) => {
      if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
      if (path.includes("section=completed")) return { value: completedPage, etag: '"c"' };
      return { value: { ...poll, operator_actions: open }, etag: `"b${open.length}"` };
    });
    // Core closes the Work item, so the next poll no longer lists it.
    request.mockImplementation(async (path: string) => { open = open.filter((item) => !path.includes(`/work/${item.id}/`)); return {}; });
  });

  it("shows open requests at the top of the board with urgency, device, reason, goal and check", async () => {
    mount();
    const board = screen.getByRole("region", { name: "Status board" });
    const panel = await within(board).findByRole("region", { name: "Needs you" });
    // Above every goal chat.
    const firstCard = within(board).getAllByRole("article")[0];
    expect(panel.compareDocumentPosition(firstCard) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const items = within(panel).getAllByRole("listitem");
    expect(items.map((item) => item.getAttribute("data-urgency"))).toEqual(["blocking", "low"]);
    expect(within(items[0]).getByText("Blocking")).toBeVisible();
    expect(within(items[0]).getByText("Restart host")).toBeVisible();
    expect(within(items[0]).getByText("lab-host-1")).toBeVisible();
    expect(within(items[0]).getByText("The host stopped answering after an update.")).toBeVisible();
    expect(within(items[0]).getByRole("link", { name: "Fuzz parser · blocked" })).toHaveAttribute("href", "/projects/p1/workbench?view=chat&session=Fuzz%20parser");
    expect(within(items[0]).getByText("Auto-check tcp_reachable: not yet (connection refused)")).toBeVisible();
    expect(within(items[1]).getByText("Needs you: plug the probe back in")).toBeVisible();
    expect(within(items[1]).getByText("No auto-check: mark it done when finished")).toBeVisible();
  });

  it("marks a request done or cancels it with a Work check-in", async () => {
    mount();
    const panel = await screen.findByRole("region", { name: "Needs you" });
    await userEvent.click(within(panel).getByRole("button", { name: "Mark done: Needs you: Restart host on lab-host-1" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
    expect(request.mock.calls[0][0]).toBe("engagements/p1/work/w1/updates");
    expect(JSON.parse(request.mock.calls[0][1].body)).toMatchObject({ status: "done", summary: "Operator marked this request done from the Butler board." });
    await waitFor(() => expect(within(panel).queryByText("lab-host-1")).not.toBeInTheDocument());

    await userEvent.click(within(panel).getByRole("button", { name: "Cancel request: Needs you: plug the probe back in" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    expect(JSON.parse(request.mock.calls[1][1].body)).toMatchObject({ status: "done", summary: "Operator cancelled this request from the Butler board." });
  });

  it("keeps a request and says so when Core refuses the check-in", async () => {
    request.mockRejectedValueOnce(new Error("Work item changed."));
    mount();
    const panel = await screen.findByRole("region", { name: "Needs you" });
    await userEvent.click(within(panel).getByRole("button", { name: "Mark done: Needs you: Restart host on lab-host-1" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("Work item changed. The request is still open.");
    expect(within(panel).getByText("lab-host-1")).toBeVisible();
  });
});
