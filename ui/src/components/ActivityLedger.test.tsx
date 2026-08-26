import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ActivityLedger } from "./ActivityLedger";
import type { ActivityLedgerViewModel } from "./activityLedger";

function model(overrides: Partial<ActivityLedgerViewModel> = {}): ActivityLedgerViewModel {
  return {
    title: "Work summary",
    status: "active",
    currentAction: "Saving verified findings.",
    actionCount: 2,
    attentionCount: 0,
    artifactCount: 0,
    phases: [{ key: "execution", label: "Tools and execution", status: "active", completed: 1, total: 2 }],
    entries: [{
      id: "tool-2",
      source: "harness",
      phase: "execution",
      status: "active",
      label: "Save finding",
      summary: "Saving verified findings.",
      sequence: 2,
      countsAsAction: true,
      artifactIds: [],
      evidenceIds: [],
      outputs: [],
      payload: {},
    }],
    ...overrides,
  };
}

describe("ActivityLedger", () => {
  it("shows current work and phases while keeping audit detail collapsed", async () => {
    const user = userEvent.setup();
    render(<ActivityLedger model={model()} />);
    const ledger = screen.getByRole("region", { name: "Work summary" });
    expect(within(ledger).getByText("Saving verified findings.")).toBeVisible();
    expect(within(ledger).getByRole("list", { name: "Work phases" })).toBeVisible();
    expect(within(ledger).queryByText("Newest first")).toBeNull();
    await user.click(within(ledger).getByRole("button", { name: "Show activity" }));
    expect(within(ledger).getByText("Newest first")).toBeVisible();
    expect(within(ledger).getByRole("button", { name: "Hide activity" })).toHaveAttribute("aria-expanded", "true");
  });

  it("turns completed work into a compact receipt", () => {
    render(<ActivityLedger model={model({ status: "complete", currentAction: undefined, artifactCount: 2, durationMs: 18_000 })} />);
    const ledger = screen.getByRole("region", { name: "Work summary" });
    expect(within(ledger).getByText("Completed", { selector: ".activity-ledger-receipt strong" })).toBeVisible();
    expect(within(ledger).getByText("2 actions · 2 artifacts · 18s")).toBeVisible();
    expect(within(ledger).queryByRole("list", { name: "Work phases" })).toBeNull();
  });

  it("does not force-close detail when live work completes", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ActivityLedger model={model()} />);
    await user.click(screen.getByRole("button", { name: "Show activity" }));
    rerender(<ActivityLedger model={model({ status: "complete", currentAction: undefined })} />);
    expect(screen.getByText("Newest first")).toBeVisible();
    expect(screen.getByRole("button", { name: "Hide activity" })).toHaveAttribute("aria-expanded", "true");
  });

  it("keeps failures visible without opening the audit", () => {
    const failed = model({
      status: "failed",
      attentionCount: 1,
      entries: [{
        ...model().entries[0],
        id: "failed-tool",
        status: "failed",
        label: "Verify TLS boundary",
        summary: "The verification command exited with status 1.",
        brief: "The verification command exited with status 1.",
      }],
    });
    render(<ActivityLedger model={failed} />);
    const attention = screen.getByLabelText("Activity requiring attention");
    expect(within(attention).getByText("Verify TLS boundary")).toBeVisible();
    expect(within(attention).getByText("The verification command exited with status 1.")).toBeVisible();
    expect(screen.queryByText("Newest first")).toBeNull();
  });
});
