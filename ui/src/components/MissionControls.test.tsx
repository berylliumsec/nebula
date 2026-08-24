import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DialogProvider } from "./DialogSystem";
import { NewMissionButton } from "./MissionControls";

const startMission = vi.fn().mockResolvedValue({ id: "run-1" });
const harness = {
  id: "harness-1",
  name: "Codex",
  kind: "codex_app_server",
  enabled: true,
  healthy: true,
  models: ["gpt-5.6-sol"],
  modelOptions: [{
    model: "gpt-5.6-sol",
    reasoningEfforts: [{ id: "high", label: "High", description: "Deeper review" }],
    defaultReasoningEffort: "high",
    serviceTiers: [{ id: "priority", label: "Priority", description: "Faster execution" }],
    defaultServiceTier: "priority",
  }],
  localOnly: true,
  permitsSensitiveData: true,
  nativeCapabilities: { workspaceAccess: "write", shell: true },
};
const api = {
  listHarnesses: vi.fn().mockResolvedValue([harness]),
  listMcpServers: vi.fn().mockResolvedValue([]),
  listHarnessSessions: vi.fn().mockResolvedValue([]),
  getAutomationRuntime: vi.fn().mockResolvedValue({ configured: false, ready: false, detail: "Optional command runtime unavailable" }),
};

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({
    api,
    coreState: "online",
    engagement: { id: "engagement-1" },
    previewMode: false,
    providers: [],
    reverifyProvider: vi.fn(),
    startMission,
  }),
}));

describe("NewMissionButton", () => {
  it("submits frozen harness options, durable stages, and a recurring schedule", async () => {
    const user = userEvent.setup();
    const scheduledLocal = "2099-08-24T09:30";
    startMission.mockClear();
    render(<DialogProvider><NewMissionButton /></DialogProvider>);
    const trigger = await screen.findByRole("button", { name: "Automate task" });
    await waitFor(() => expect(trigger).toBeEnabled());
    await user.click(trigger);
    await user.type(screen.getByLabelText("Mission name"), "Weekly security review");
    await user.type(screen.getByLabelText("Objective"), "Review the bounded project");
    await user.click(screen.getByText("Advanced"));
    expect(screen.getByText("Supervised security automation")).toBeInTheDocument();
    expect(screen.getByText("Harness native")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Model")).toHaveValue("gpt-5.6-sol"));
    expect(screen.getByLabelText("Mission harness effort")).toHaveValue("high");
    expect(screen.getByLabelText("Mission harness speed")).toHaveValue("priority");
    await user.click(screen.getByRole("button", { name: "Add stage" }));
    const stageObjectives = screen.getAllByLabelText("Objective");
    await user.type(stageObjectives[1], "Verify the strongest observation");
    fireEvent.change(screen.getByLabelText("Start time"), { target: { value: scheduledLocal } });
    await user.selectOptions(screen.getByLabelText("Repeat"), "86400");
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByRole("button", { name: "Automate task" })).toBeEnabled());
    await user.click(dialog.getByRole("button", { name: "Automate task" }));
    await waitFor(() => expect(startMission).toHaveBeenCalled());
    expect(startMission).toHaveBeenCalledWith(expect.objectContaining({
      backend: "harness",
      harnessProfileId: "harness-1",
      model: "gpt-5.6-sol",
      harnessReasoningEffort: "high",
      harnessServiceTier: "priority",
      stages: [{ title: "Stage 1", objective: "Verify the strongest observation" }],
      scheduledFor: new Date(`${scheduledLocal}:00`).toISOString(),
      repeatIntervalSeconds: 86_400,
    }));
  });
});
