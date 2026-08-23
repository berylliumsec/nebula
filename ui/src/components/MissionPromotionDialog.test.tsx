import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DialogProvider } from "./DialogSystem";
import { MissionPromotionDialog } from "./MissionPromotionDialog";

const createFinding = vi.fn().mockResolvedValue({ id: "finding-1" });
const createReport = vi.fn().mockResolvedValue({ id: "report-1" });

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({
    createFinding,
    createReport,
    engagement: { id: "engagement-1" },
  }),
}));

const run = {
  id: "run-1",
  engagementId: "engagement-1",
  title: "Reviewed perimeter",
  status: "complete" as const,
  updatedAt: "2026-08-23T12:00:00Z",
  completedTasks: 2,
  totalTasks: 2,
};

describe("MissionPromotionDialog", () => {
  beforeEach(() => {
    createFinding.mockClear();
    createReport.mockClear();
  });

  it("creates only an operator-reviewed candidate with Mission provenance", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><MissionPromotionDialog run={run} summary="Verified one bounded observation." /></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Create reviewed draft" }));
    await user.clear(screen.getByLabelText("Title"));
    await user.type(screen.getByLabelText("Title"), "Candidate exposure");
    await user.selectOptions(screen.getByLabelText("Initial severity"), "medium");
    await user.click(screen.getByRole("button", { name: "Create candidate" }));
    expect(createFinding).toHaveBeenCalledWith(expect.objectContaining({
      engagementId: "engagement-1",
      title: "Candidate exposure",
      severity: "medium",
      sourceRunId: "run-1",
    }));
    expect(createReport).not.toHaveBeenCalled();
  });

  it("creates an unsigned report draft rather than promoting a claim", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><MissionPromotionDialog run={run} summary="Draftable Mission result." /></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Create reviewed draft" }));
    await user.click(screen.getByRole("button", { name: "Report draft" }));
    await user.click(screen.getByRole("button", { name: "Create report draft" }));
    expect(createReport).toHaveBeenCalledWith(expect.objectContaining({
      engagementId: "engagement-1",
      status: "draft",
      sourceRunId: "run-1",
    }));
  });
});
