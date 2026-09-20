import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DialogProvider } from "./DialogSystem";
import { localDateTimeInputValue, NewMissionButton } from "./MissionControls";

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
  getAutomationRuntime: vi.fn().mockResolvedValue({ configured: true, ready: true, detail: "Pinned automation runtime ready" }),
};

let engagementId = "engagement-1";

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({
    api,
    coreState: "online",
    engagement: { id: engagementId },
    previewMode: false,
    providers: [],
    reverifyProvider: vi.fn(),
    startMission,
  }),
}));

const mcpServer = {
  id: "mcp-1",
  name: "Recon tools",
  transport: "stdio",
  arguments: [],
  authMode: "none",
  enabled: true,
  required: false,
  trustedStdio: true,
  defaultApproval: "risk_based",
  toolOverrides: {},
  tools: [],
};

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  const view = render(<DialogProvider><NewMissionButton /></DialogProvider>);
  const trigger = await screen.findByRole("button", { name: "Automate task" });
  await waitFor(() => expect(trigger).toBeEnabled());
  await user.click(trigger);
  return { ...view, dialog: within(screen.getByRole("dialog")) };
}

describe("NewMissionButton", () => {
  afterEach(() => {
    engagementId = "engagement-1";
    startMission.mockClear();
  });

  it("keeps the dialog open while a mission is starting so a failure stays visible", async () => {
    const user = userEvent.setup();
    let rejectStart: (error: Error) => void = () => undefined;
    startMission.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectStart = reject; }));
    const { dialog } = await openDialog(user);
    await user.type(screen.getByLabelText("Mission name"), "Perimeter review");
    await user.type(screen.getByLabelText("Objective"), "Review the bounded project");
    await waitFor(() => expect(screen.getByLabelText("Model")).toHaveValue("gpt-5.6-sol"));
    await waitFor(() => expect(dialog.getByRole("button", { name: "Automate task" })).toBeEnabled());
    await user.click(dialog.getByRole("button", { name: "Automate task" }));
    await waitFor(() => expect(startMission).toHaveBeenCalled());

    expect(dialog.getByRole("button", { name: "Close automation dialog" })).toBeDisabled();
    expect(dialog.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await act(async () => { rejectStart(new Error("Core refused the mission.")); });
    expect(await dialog.findByText("Core refused the mission.")).toBeVisible();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(dialog.getByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it("keeps typed execution limits when an MCP server is ticked", async () => {
    const user = userEvent.setup();
    api.listMcpServers.mockResolvedValueOnce([mcpServer]);
    await openDialog(user);
    await user.click(screen.getByText("Advanced"));
    await waitFor(() => expect(screen.getByText("Ready")).toBeInTheDocument());
    const limit = screen.getByLabelText("Maximum execution calls");
    const concurrency = screen.getByLabelText("Maximum concurrency");
    await user.type(limit, "5");
    await user.clear(concurrency);
    await user.type(concurrency, "2");
    expect(limit).toHaveValue(5);
    expect(concurrency).toHaveValue(2);

    await user.click(screen.getByRole("checkbox", { name: /Recon tools/ }));
    expect(screen.getByRole("checkbox", { name: /Recon tools/ })).toBeChecked();
    expect(limit).toHaveValue(5);
    expect(concurrency).toHaveValue(2);
  });

  it("re-checks the automation runtime for the project selected while the dialog is open", async () => {
    const user = userEvent.setup();
    api.getAutomationRuntime.mockClear();
    const { rerender } = await openDialog(user);
    await waitFor(() => expect(api.getAutomationRuntime).toHaveBeenCalledWith(undefined, "engagement-1"));

    engagementId = "engagement-2";
    rerender(<DialogProvider><NewMissionButton /></DialogProvider>);
    await waitFor(() => expect(api.getAutomationRuntime).toHaveBeenCalledWith(undefined, "engagement-2"));
  });

  it("builds the earliest start time from the operator's local clock", async () => {
    vi.stubEnv("TZ", "Etc/GMT+5");
    try {
      expect(new Date(2026, 5, 15, 8, 5).getTimezoneOffset()).toBe(300);
      expect(localDateTimeInputValue(new Date(2026, 5, 15, 8, 5))).toBe("2026-06-15T08:05");
      const user = userEvent.setup();
      await openDialog(user);
      const before = localDateTimeInputValue(new Date(Date.now() + 60_000));
      await user.click(screen.getByText("Advanced"));
      const min = screen.getByLabelText("Start time").getAttribute("min");
      const after = localDateTimeInputValue(new Date(Date.now() + 60_000));
      expect([before, after]).toContain(min);
    } finally {
      vi.unstubAllEnvs();
    }
  });

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
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Bash and process I/O use the project’s selected execution mode.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Model")).toHaveValue("gpt-5.6-sol"));
    expect(screen.getByLabelText("Mission harness effort")).toHaveValue("high");
    expect(screen.getByLabelText("Mission harness speed")).toHaveValue("priority");
    expect(screen.getByLabelText("Duration (minutes)")).toHaveValue(null);
    expect(screen.getByLabelText("Token limit")).toHaveValue(null);
    expect(screen.getByLabelText("Cost limit (USD)")).toHaveValue(null);
    expect(screen.getByLabelText("Maximum execution calls")).toHaveValue(null);
    await user.click(screen.getByRole("button", { name: "Add stage" }));
    const stageObjectives = screen.getAllByLabelText("Objective");
    await user.type(stageObjectives[1], "Verify the strongest observation");
    fireEvent.change(screen.getByLabelText("Start time"), { target: { value: scheduledLocal } });
    await user.selectOptions(screen.getByLabelText("Repeat"), "86400");
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByRole("button", { name: "Automate task" })).toBeEnabled());
    await user.click(dialog.getByRole("button", { name: "Automate task" }));
    await waitFor(() => expect(startMission).toHaveBeenCalled());
    const submitted = startMission.mock.calls[0][0] as Record<string, unknown>;
    expect(submitted).not.toHaveProperty("maxDurationSeconds");
    expect(submitted).not.toHaveProperty("maxTokens");
    expect(submitted).not.toHaveProperty("maxCostUsd");
    expect(submitted).not.toHaveProperty("maxToolCalls");
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
