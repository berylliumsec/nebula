import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { GuideProgress } from "../api/types";
import { GuideProvider, useGuides } from "./GuideProvider";
import { guideDestination, placeCard } from "./GuideOverlay";
import { guideCatalog } from "./catalog";
import { guideMatches } from "./GuidesDrawer";

const workspace = vi.hoisted(() => ({
  api: {
    listGuideProgress: vi.fn(),
    saveGuideProgress: vi.fn(),
    createGuideStarterFiles: vi.fn(),
    listNativeHooks: vi.fn(),
    listSkills: vi.fn(),
    getProjectInstructionsStatus: vi.fn(),
  },
  engagement: { id: "project-1" } as { id: string } | undefined,
  workspaceState: "ready",
}));
vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));

afterEach(cleanup);

function progress(guideId: string, stepIndex: number, revision = 1, status: GuideProgress["status"] = "in_progress"): GuideProgress {
  return { guideId, stepIndex, revision, status, updatedAt: "2026-09-19T10:00:00Z" };
}

function Location() {
  const location = useLocation();
  return <output aria-label="location">{`${location.pathname}${location.search}${location.hash}`}</output>;
}

function OpenHub() {
  const { openHub } = useGuides();
  return <button type="button" onClick={openHub}>Open hub</button>;
}

function renderGuides(path = "/projects/project-1/workbench?view=chat&session=s1") {
  return render(<MemoryRouter initialEntries={[path]}>
    <GuideProvider><OpenHub /><Location /></GuideProvider>
  </MemoryRouter>);
}

describe("guide routing helpers", () => {
  it("merges a same-page route into the current URL so the open conversation survives", () => {
    const current = { pathname: "/projects/p/workbench", search: "?session=s1&view=terminal", hash: "" };
    expect(guideDestination("/projects/p/workbench?view=chat&drawer=context", current))
      .toBe("/projects/p/workbench?session=s1&view=chat&drawer=context");
    expect(guideDestination("/projects/p/workbench?view=terminal", current)).toBeUndefined();
    expect(guideDestination("/settings#provider-settings", current)).toBe("/settings#provider-settings");
  });

  it("places the card beside the target and inside the viewport", () => {
    Object.assign(window, { innerWidth: 1440, innerHeight: 900 });
    expect(placeCard({ top: 600, left: 800, width: 300, height: 100 }, { width: 380, height: 260 }))
      .toEqual({ top: 600, left: 404 });
    expect(placeCard({ top: 850, left: 10, width: 300, height: 40 }, { width: 380, height: 260 }))
      .toEqual({ top: 624, left: 326 });
    Object.assign(window, { innerWidth: 390 });
    expect(placeCard({ top: 10, left: 10, width: 100, height: 40 }, { width: 380, height: 260 })).toBeUndefined();
    Object.assign(window, { innerWidth: 1024, innerHeight: 768 });
  });

  it("finds guides by feature words", () => {
    const hooks = guideCatalog.find(guide => guide.id === "lifecycle-hooks")!;
    expect(guideMatches(hooks, "hook.json")).toBe(true);
    expect(guideMatches(hooks, "phone")).toBe(false);
  });

  it("points every step at a control the product labels, and never at a missing guide action", () => {
    const anchors = new Set(["lifecycle-hooks", "hook-outcomes", "composer", "shared-skills", "workspace-controls", "add-provider", "device-pairing", "command-palette",
      "assistant-runtime", "attach-files", "terminal-toggle", "project-policy", "message-actions", "transcript-search", "operator-context", "goal-panel", "mcp-settings", "mcp-turn", "knowledge-status", "tool-assistance", "continue-mission"]);
    const ids = guideCatalog.map(guide => guide.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const step of guideCatalog.flatMap(guide => guide.steps)) {
      if (step.target) expect(anchors).toContain(step.target);
    }
  });

  it("keeps guide identities valid for Core", () => {
    for (const guide of guideCatalog) {
      expect(guide.id).toMatch(/^[a-z0-9][a-z0-9-]{0,79}$/);
      expect(guide.steps.length).toBeGreaterThan(0);
      expect(guide.steps.length).toBeLessThanOrEqual(65);
    }
  });
});

describe("GuideProvider", () => {
  beforeEach(() => {
    workspace.engagement = { id: "project-1" };
    for (const mock of Object.values(workspace.api)) mock.mockReset();
    workspace.api.listGuideProgress.mockResolvedValue([]);
    workspace.api.saveGuideProgress.mockImplementation(async (guideId: string, value: { stepIndex: number; status: GuideProgress["status"]; expectedRevision: number }) =>
      progress(guideId, value.stepIndex, value.expectedRevision + 1, value.status));
    workspace.api.listNativeHooks.mockResolvedValue([]);
  });

  it("lists page guides first, starts one, and records each step in Core", async () => {
    const user = userEvent.setup();
    renderGuides();
    await user.click(screen.getByRole("button", { name: "Open hub" }));
    const hub = await screen.findByRole("dialog", { name: "Guides" });
    const forPage = within(hub).getByRole("region", { name: "For this page" });
    expect(within(forPage).getByRole("button", { name: /Run your own script on every chat turn\. Start/ })).toBeInTheDocument();

    await user.click(within(forPage).getByRole("button", { name: /Run your own script on every chat turn/ }));
    expect(screen.queryByRole("dialog", { name: "Guides" })).not.toBeInTheDocument();
    expect(await screen.findByRole("dialog", { name: "What a lifecycle hook does" })).toBeInTheDocument();
    await waitFor(() => expect(workspace.api.saveGuideProgress).toHaveBeenCalledWith("lifecycle-hooks", { status: "in_progress", stepIndex: 0, expectedRevision: 0 }));

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("dialog", { name: "Create the hook files" })).toBeInTheDocument();
    await waitFor(() => expect(workspace.api.saveGuideProgress).toHaveBeenLastCalledWith("lifecycle-hooks", { status: "in_progress", stepIndex: 1, expectedRevision: 1 }));
  });

  it("resumes where Core says the operator stopped, and replays completed guides from the start", async () => {
    workspace.api.listGuideProgress.mockResolvedValue([progress("agents-md", 2, 4), progress("shortcuts", 4, 2, "completed")]);
    const user = userEvent.setup();
    renderGuides();
    await user.click(screen.getByRole("button", { name: "Open hub" }));
    const hub = await screen.findByRole("dialog", { name: "Guides" });
    expect(within(hub).getByText(`1 of ${guideCatalog.length} done`)).toBeInTheDocument();
    await user.click(within(hub).getByRole("button", { name: /Give the assistant standing project instructions\. Resume at step 3 \/ 3/ }));
    expect(await screen.findByRole("dialog", { name: "How it is used" })).toBeInTheDocument();
  });

  it("creates starter files, opens them in Code, and confirms Core discovered the hook", async () => {
    workspace.api.listGuideProgress.mockResolvedValue([progress("lifecycle-hooks", 1, 3)]);
    workspace.api.createGuideStarterFiles.mockResolvedValue({ kind: "hook", paths: [".agents/hooks/audit/hook.json", ".agents/hooks/audit/run.sh"] });
    const user = userEvent.setup();
    renderGuides();
    await user.click(screen.getByRole("button", { name: "Open hub" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Guides" })).getByRole("button", { name: /^Run your own script/ }));
    const card = await screen.findByRole("dialog", { name: "Create the hook files" });
    expect(within(card).getByRole("status")).toHaveTextContent("Waiting for .agents/hooks/audit/hook.json");

    workspace.api.listNativeHooks.mockResolvedValue([{ id: "audit", source: "project", path: "/w/.agents/hooks/audit", manifest: { version: 1, name: "Audit", description: "", events: [], timeoutSeconds: 10, sideEffects: "workspace", failurePolicy: "continue" } }]);
    await user.click(within(card).getByRole("button", { name: "Create starter files" }));
    expect(workspace.api.createGuideStarterFiles).toHaveBeenCalledWith("project-1", "hook", "audit");
    expect(screen.getByLabelText("location")).toHaveTextContent("/projects/project-1/workbench?view=code&session=s1&openFile=.agents%2Fhooks%2Faudit%2Fhook.json");
    expect(await within(card).findByText("Nebula lists “Audit” for this project.")).toBeInTheDocument();
  });

  it("shows why an existing hook fails instead of a generic error", async () => {
    workspace.api.listGuideProgress.mockResolvedValue([progress("lifecycle-hooks", 1, 1)]);
    workspace.api.listNativeHooks.mockRejectedValue(new ApiError("hook executable is not executable: audit", 409));
    const user = userEvent.setup();
    renderGuides();
    await user.click(screen.getByRole("button", { name: "Open hub" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Guides" })).getByRole("button", { name: /^Run your own script/ }));
    expect(await screen.findByText("hook executable is not executable: audit")).toBeInTheDocument();
  });

  it("takes Core's newer revision when another device moved the guide on", async () => {
    workspace.api.listGuideProgress
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([])
      .mockResolvedValue([progress("shortcuts", 3, 5)]);
    workspace.api.saveGuideProgress
      .mockRejectedValueOnce(new ApiError("revision conflict", 409))
      .mockResolvedValue(progress("shortcuts", 0, 6));
    const user = userEvent.setup();
    renderGuides("/settings");
    await user.click(screen.getByRole("button", { name: "Open hub" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Guides" })).getByRole("button", { name: /^Shortcuts and the command palette\./ }));
    await waitFor(() => expect(workspace.api.saveGuideProgress).toHaveBeenLastCalledWith("shortcuts", { status: "in_progress", stepIndex: 0, expectedRevision: 5 }));
  });

  it("asks for a project before project steps and minimizes to a resume control", async () => {
    workspace.engagement = undefined;
    const user = userEvent.setup();
    renderGuides("/settings");
    await user.click(screen.getByRole("button", { name: "Open hub" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Guides" })).getByRole("button", { name: /^Give the assistant standing project instructions/ }));
    const card = await screen.findByRole("dialog", { name: "Create AGENTS.md" });
    expect(within(card).getByRole("status")).toHaveTextContent("Open or create a project first");
    expect(within(card).queryByRole("button", { name: "Create starter files" })).not.toBeInTheDocument();

    await user.keyboard("{Escape}");
    const resume = await screen.findByRole("button", { name: "Resume guide: Give the assistant standing project instructions, step 1 of 3" });
    await user.click(resume);
    expect(await screen.findByRole("dialog", { name: "Create AGENTS.md" })).toBeInTheDocument();
  });

  it("spotlights the real control when it is on screen", async () => {
    const user = userEvent.setup();
    const { container } = renderGuides("/settings");
    const target = document.createElement("button");
    target.dataset.guide = "command-palette";
    target.getBoundingClientRect = () => ({ top: 10, left: 900, width: 120, height: 32, right: 1020, bottom: 42, x: 900, y: 10, toJSON: () => ({}) });
    container.appendChild(target);
    await user.click(screen.getByRole("button", { name: "Open hub" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Guides" })).getByRole("button", { name: /^Shortcuts and the command palette\./ }));
    await screen.findByRole("dialog", { name: "Open the command palette" });
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 250)); });
    const spotlight = document.querySelector<HTMLElement>(".guide-spotlight");
    expect(spotlight?.style.left).toBe("894px");
    expect(spotlight?.style.width).toBe("132px");
  });
});
