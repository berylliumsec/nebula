import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DialogProvider } from "../components/DialogSystem";
import { MobileMorePanel } from "../components/MobileMorePanel";
import { SideNav } from "../components/SideNav";
import { AtlasPage } from "./AtlasPage";

const { workspace, request, index } = vi.hoisted(() => {
  const index = {
    generatedAt: "2026-10-07T12:00:00Z",
    source: { available: true, mapPath: "knowledge/GRAND_THREAT_PROJECT_MAP.md", mapSnapshot: "2026-08-25", statusAuthority: "nebula-work", migrationAuthority: "workspace-link" },
    counts: { grandThreatProjects: 2, openLanes: null, needsSimulation: null, documents: 1, simulations: 1 },
    grandThreatProjects: [
      { name: "AppleJPEGXL", path: "projects/applejpegxl", missing: false, roles: ["focused"], surfaces: ["Image files"], lastWorkedAt: null,
        workspace: { linked: true, engagementId: "project-1" }, workflow: { status: "in_progress", summary: "Parser review", nextStep: "Check capacity", updatedAt: "2026-10-07T11:00:00Z", source: "nebula-work" },
        migration: { status: "linked", label: "Linked", detail: "Nebula project" },
        v2Migration: { status: "audited", label: "V2 admitted", detail: "Gate passed", validation: "validated", auditedAt: "2026-08-20T12:00:00Z", auditDetail: "Reviewed", remainingGate: null },
        binaries: { count: 1, totalBytes: 2048, files: [] }, lanes: { indexed: true, total: 3, open: 2, needsSimulation: 1, readyForRuntime: 0, stale: 0 } },
      { name: "Unscaffolded target", path: "projects/missing", missing: true, roles: ["supporting"], surfaces: ["IPC"], lastWorkedAt: null,
        workspace: { linked: false, engagementId: null }, workflow: { status: "unknown", summary: "", nextStep: null, updatedAt: null, source: "unknown" },
        migration: { status: "missing", label: "Missing", detail: "No project" },
        v2Migration: { status: "unknown", label: "Unknown", detail: "", validation: "not_audited", auditedAt: null, auditDetail: "", remainingGate: null },
        binaries: { count: null, totalBytes: null, files: [] }, lanes: { indexed: false, total: null, open: null, needsSimulation: null, readyForRuntime: null, stale: null } },
    ],
    documents: [{ id: "doc-1", title: "Grand Threat Map", kind: "threat", featured: true, project: "Research", path: "knowledge/GRAND_THREAT_PROJECT_MAP.md", excerpt: "Research priorities", headings: ["Overview"], updatedAt: "2026-08-25T12:00:00Z" }],
    simulations: [{ runKey: "run-1", project: "AppleJPEGXL", status: "bounded", feasibility: "unknown", decisionImpact: "More proof needed", summary: "No validated impact", updatedAt: "2026-10-07T11:00:00Z" }],
  };
  const request = vi.fn(async (path: string): Promise<unknown> => {
    if (path === "atlas") return index;
    if (path === "atlas/documents/doc-1") return { ...index.documents[0], content: "# Grand Threat Map\n\nResearch priorities", truncated: false };
    throw new Error("Document missing");
  });
  const engagement = { id: "project-1", name: "AppleJPEGXL", status: "active", workspacePath: "/research" };
  return { index, request, workspace: { api: { request }, coreState: "online", reconnect: vi.fn(), engagement, engagements: [engagement], archivedEngagements: [], activeOperator: undefined, createEngagement: vi.fn(), setEngagementArchived: vi.fn(), deleteArchivedEngagement: vi.fn() } };
});

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));
afterEach(cleanup);
beforeEach(() => { request.mockClear(); });

function CurrentLocation() {
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}{location.search}</output>;
}

function openAtlas(path = "/atlas") {
  return render(<MemoryRouter initialEntries={[path]}><DialogProvider><Routes><Route path="/atlas" element={<AtlasPage />} /></Routes><CurrentLocation /></DialogProvider></MemoryRouter>);
}

describe("Intel Atlas operator journey", () => {
  it("explains an unavailable research root without showing false zero counts", async () => {
    request.mockImplementationOnce(async () => ({
      ...index,
      source: { ...index.source, available: false, mapPath: "" },
      counts: { grandThreatProjects: 0, openLanes: 0, needsSimulation: 0, documents: 0, simulations: 0 },
      grandThreatProjects: [], documents: [], simulations: [],
    }));
    openAtlas();
    expect(await screen.findByRole("heading", { name: "Research map unavailable" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Projects" })).toHaveAttribute("href", "/projects");
    expect(screen.queryByRole("region", { name: "Atlas summary" })).not.toBeInTheDocument();
  });

  it("is a visible, current sidebar destination", () => {
    render(<MemoryRouter initialEntries={["/atlas"]}><DialogProvider><SideNav collapsed={false} open={false} setOpen={vi.fn()} onNavigate={vi.fn()} /><CurrentLocation /></DialogProvider></MemoryRouter>);
    const atlas = screen.getByRole("link", { name: "Intel Atlas" });
    expect(atlas).toHaveAttribute("href", "/atlas");
    expect(atlas).toHaveAttribute("aria-current", "page");
  });

  it("is discoverable from the phone More menu", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/projects/project-1/workbench"]}><DialogProvider><MobileMorePanel view="chat" onSelectView={vi.fn()} onFocusMode={vi.fn()} onClose={vi.fn()} /><CurrentLocation /></DialogProvider></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Intel Atlas" }));
    expect(screen.getByRole("status", { name: "Current location" })).toHaveTextContent("/atlas");
  });

  it("shows linked Work status and distinguishes missing research evidence from zero", async () => {
    openAtlas();
    expect(await screen.findByRole("heading", { name: "AppleJPEGXL" })).toBeVisible();
    const linked = screen.getByRole("heading", { name: "AppleJPEGXL" }).closest("article")!;
    expect(within(linked).getByRole("link", { name: "Open Work" })).toHaveAttribute("href", "/projects/project-1/work");
    expect(within(linked).getByText("in progress")).toBeVisible();
    expect(within(linked).getByText("Check capacity")).toBeVisible();
    expect(within(linked).getByText(/Audited/)).toBeVisible();
    const missing = screen.getByRole("heading", { name: "Unscaffolded target" }).closest("article")!;
    expect(within(missing).getByText("No linked Nebula project")).toBeVisible();
    expect(within(missing).getByText("No compiled lane index")).toBeVisible();
    expect(within(missing).queryByRole("link", { name: "Open Work" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Atlas summary" })).getByText("Need simulation").previousElementSibling).toHaveTextContent("Unknown");
  });

  it("keeps view, search, and role in the URL and opens a bounded document preview", async () => {
    const user = userEvent.setup();
    openAtlas("/atlas?role=supporting");
    expect(await screen.findByRole("heading", { name: "Unscaffolded target" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "AppleJPEGXL" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Maps & inventories" }));
    expect(screen.getByRole("status", { name: "Current location" })).toHaveTextContent("view=maps");
    await user.type(screen.getByRole("searchbox", { name: "Search" }), "Grand Threat");
    expect(screen.getByRole("status", { name: "Current location" })).toHaveTextContent("q=Grand+Threat");
    await user.click(screen.getByRole("button", { name: /Grand Threat Map/ }));
    const dialog = await screen.findByRole("dialog", { name: "Grand Threat Map" });
    expect(within(dialog).getByText(/Research priorities/)).toBeVisible();
    expect(screen.getByRole("status", { name: "Current location" })).toHaveTextContent("document=doc-1");
    await user.click(within(dialog).getByRole("button", { name: "Close document" }));
    expect(screen.queryByRole("dialog", { name: "Grand Threat Map" })).not.toBeInTheDocument();
  });

  it("separates a Work item without a check-in from a linked project without a status item", async () => {
    request.mockImplementationOnce(async () => ({
      ...index,
      grandThreatProjects: [
        { ...index.grandThreatProjects[0], workflow: { ...index.grandThreatProjects[0].workflow, updatedAt: null } },
        { ...index.grandThreatProjects[1], workspace: { linked: true, engagementId: "project-2" } },
      ],
    }));
    openAtlas();
    const withItem = (await screen.findByRole("heading", { name: "AppleJPEGXL" })).closest("article")!;
    const withoutItem = screen.getByRole("heading", { name: "Unscaffolded target" }).closest("article")!;
    expect(within(withItem).getByText("Work item has no check-in yet")).toBeVisible();
    expect(within(withoutItem).getByText("No Research status item in Work")).toBeVisible();
  });

  it("keeps the last projection visible after refresh fails and allows retry", async () => {
    const user = userEvent.setup();
    openAtlas();
    expect(await screen.findByRole("heading", { name: "AppleJPEGXL" })).toBeVisible();
    request.mockRejectedValueOnce(new Error("Corpus busy"));
    await user.click(screen.getByRole("button", { name: "Refresh Intel Atlas" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Corpus busy");
    expect(screen.getByRole("heading", { name: "AppleJPEGXL" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });
});
