import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { StructuredResultSummary } from "../../api/types";
import { AgentViewPanel, type AgentViewPanelProps } from "./AgentViewPanel";
import { clampRect, MIN_HEIGHT, MIN_WIDTH, readPlacement, readRect, writePlacement } from "./agentViewGeometry";

vi.mock("../../diagnostics", () => ({
  logCaughtDiagnostic: vi.fn(),
  DiagnosticErrorNotice: ({ title, error }: { title: string; error: string }) => <div role="alert">{title}: {error}</div>,
}));

const listStructuredResults = vi.fn();
const getStructuredResult = vi.fn();
const api = { listStructuredResults, getStructuredResult } as unknown as ApiClient;

function summary(id: string, sequence: number): StructuredResultSummary {
  return {
    id,
    projectId: "project-1",
    title: `Step ${sequence}`,
    summary: "",
    producer: "dashboard.publish",
    origin: "agent",
    labels: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    revision: 1,
    stats: { byteSize: 40, nodeCount: 4, maxDepth: 2, rootType: "object", topLevelCount: 2 },
    hasHints: false,
    preview: [],
    stream: "goal-1",
    streamLabel: "Refactor auth",
    sequence,
    chatSessionId: "session-1",
  };
}

const viewport = { left: 0, top: 0, width: 1280, height: 800 };

function panel(overrides: Partial<AgentViewPanelProps> = {}) {
  const props: AgentViewPanelProps = {
    api,
    projectId: "project-1",
    sessionId: "session-1",
    minimized: false,
    unseen: 0,
    onMinimize: vi.fn(),
    onRestore: vi.fn(),
    onClose: vi.fn(),
    onDock: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<MemoryRouter><AgentViewPanel {...props} /></MemoryRouter>) };
}

function setNarrow(narrow: boolean) {
  vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
    matches: narrow && query.includes("760px"),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

describe("where the Agent view sits", () => {
  beforeEach(() => localStorage.clear());

  it("floats until the operator docks it, and remembers the choice", () => {
    expect(readPlacement()).toBe("floating");
    writePlacement("docked");
    expect(readPlacement()).toBe("docked");
  });

  it("never shrinks past its minimum, outgrows the window, or leaves the screen", () => {
    expect(clampRect({ x: 0, y: 0, width: 10, height: 10 }, viewport)).toEqual({ x: 12, y: 12, width: MIN_WIDTH, height: MIN_HEIGHT });
    const huge = clampRect({ x: -500, y: 9000, width: 5000, height: 5000 }, viewport);
    expect(huge).toEqual({ x: 12, y: 12, width: 1256, height: 776 });
    const offRight = clampRect({ x: 1200, y: 100, width: 480, height: 600 }, viewport);
    expect(offRight.x + offRight.width).toBe(1268);
  });

  it("opens under the toolbar and clear of the composer when there is room", () => {
    const tall = { left: 0, top: 0, width: 1440, height: 900 };
    const rect = readRect(tall, { below: 140, above: 632 });
    expect(rect.y).toBe(152);
    expect(rect.y + rect.height).toBe(620);
    // Without room for both, the panel keeps its minimum and overlaps.
    const short = readRect({ ...tall, height: 500 }, { below: 140, above: 400 });
    expect(short.height).toBe(MIN_HEIGHT);
  });

  it("starts from the defaults when a saved layout is unreadable", () => {
    localStorage.setItem("nebula.agent-view.geometry", "{not json");
    const rect = readRect(viewport);
    expect(rect.width).toBe(480);
    expect(rect.x + rect.width).toBeLessThanOrEqual(viewport.width - 12);
    localStorage.setItem("nebula.agent-view.geometry", JSON.stringify({ x: "left", y: 1, width: 1, height: 1 }));
    expect(readRect(viewport).width).toBe(480);
  });

  it("survives storage that refuses to be touched", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(readPlacement()).toBe("floating");
    expect(() => writePlacement("docked")).not.toThrow();
    getItem.mockRestore();
    setItem.mockRestore();
  });
});

describe("the floating Agent view", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    setNarrow(false);
    listStructuredResults.mockResolvedValue([summary("step-2", 2), summary("step-1", 1)]);
    getStructuredResult.mockImplementation(async (_project: string, id: string) => ({ ...summary(id, id === "step-2" ? 2 : 1), result: { routes: 47 } }));
  });
  afterEach(() => setNarrow(false));

  it("is a labelled, non-modal surface that follows the newest snapshot", async () => {
    panel();
    const dialog = screen.getByRole("dialog", { name: "Agent view" });
    expect(dialog).not.toHaveAttribute("aria-modal");
    // Opening moves focus into the view.
    expect(dialog).toHaveFocus();
    expect(await screen.findByText("Following newest · 2 snapshots")).toBeInTheDocument();
    expect(await screen.findByText("Snapshot 2 of 2 · newest")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open in Results/ })).toHaveAttribute("href", "/projects/project-1/results/step-2");
  });

  it("docks, minimizes and closes on the operator's word", async () => {
    const user = userEvent.setup();
    const { props } = panel();
    await user.click(screen.getByRole("button", { name: "Dock in conversation details" }));
    expect(props.onDock).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "Minimize Agent view" }));
    expect(props.onMinimize).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "Close Agent view" }));
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("puts itself away on Escape without closing", async () => {
    const user = userEvent.setup();
    const { props } = panel();
    await user.keyboard("{Escape}");
    expect(props.onMinimize).toHaveBeenCalledOnce();
    expect(props.onClose).not.toHaveBeenCalled();
  });

  it("moves and resizes from the keyboard, and remembers where it was left", async () => {
    const user = userEvent.setup();
    panel();
    const dialog = screen.getByRole("dialog", { name: "Agent view" });
    const before = { left: parseFloat(dialog.style.left), top: parseFloat(dialog.style.top), width: parseFloat(dialog.style.width) };

    screen.getByRole("button", { name: "Move Agent view" }).focus();
    await user.keyboard("{ArrowLeft}{ArrowDown}");
    expect(parseFloat(dialog.style.left)).toBe(before.left - 24);
    expect(parseFloat(dialog.style.top)).toBe(before.top + 24);

    screen.getByRole("button", { name: "Resize Agent view" }).focus();
    await user.keyboard("{ArrowLeft}");
    expect(parseFloat(dialog.style.width)).toBe(before.width - 24);

    const saved = JSON.parse(localStorage.getItem("nebula.agent-view.geometry") ?? "{}");
    expect(saved).toMatchObject({ x: before.left - 24, y: before.top + 24, width: before.width - 24 });
  });

  it("is a sheet on a phone, with nothing to drag or dock into", async () => {
    setNarrow(true);
    panel();
    const dialog = screen.getByRole("dialog", { name: "Agent view" });
    expect(dialog).toHaveClass("sheet");
    expect(dialog.style.left).toBe("");
    expect(screen.queryByRole("button", { name: "Move Agent view" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Dock in conversation details" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resize Agent view" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Minimize Agent view" })).toBeInTheDocument();
  });
});

describe("the minimized Agent view", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    listStructuredResults.mockResolvedValue([]);
  });

  it("keeps counting what arrived and hands focus to the way back", async () => {
    const user = userEvent.setup();
    const { props } = panel({ minimized: true, unseen: 2 });
    const show = screen.getByRole("button", { name: "Show Agent view, 2 new snapshots" });
    expect(show).toHaveFocus();
    // Minimized, it reads nothing of its own: the page already counts.
    expect(listStructuredResults).not.toHaveBeenCalled();
    await user.click(show);
    expect(props.onRestore).toHaveBeenCalledOnce();
  });

  it("says it is keeping up when nothing new has arrived", () => {
    panel({ minimized: true, unseen: 0 });
    expect(screen.getByRole("button", { name: "Show Agent view, Following newest" })).toBeInTheDocument();
  });

  it("moves from the keyboard and stays where it was put", async () => {
    const user = userEvent.setup();
    panel({ minimized: true });
    screen.getByRole("button", { name: "Move minimized Agent view" }).focus();
    await user.keyboard("{ArrowUp}");
    await waitFor(() => expect(localStorage.getItem("nebula.agent-view.launcher")).not.toBeNull());
  });
});
