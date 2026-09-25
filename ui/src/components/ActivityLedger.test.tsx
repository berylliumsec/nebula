import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ActivityLedger } from "./ActivityLedger";
import type { ActivityLedgerViewModel } from "./activityLedgerModel";
import type { HarnessActivityItem } from "../pages/harnessActivity";

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
  it("shows the newest update and keeps exact prior prose under View work", async () => {
    const user = userEvent.setup();
    render(<ActivityLedger compact model={model({ entries: [], currentAction: undefined, actionCount: 0, phases: [] })} progress={{
      headline: "Waiting for delegated work",
      latest: "Recording the decision and dispatching work.",
      statusLabel: "Waiting",
      receipt: "Updates and thinking saved",
      details: <><p>First update from the provider.</p><p>Recording the decision and dispatching work.</p></>,
    }} />);
    const ledger = screen.getByRole("region", { name: "Work summary" });
    expect(within(ledger).getByText("Waiting for delegated work")).toBeVisible();
    expect(within(ledger).getByText("Recording the decision and dispatching work.")).toBeVisible();
    expect(within(ledger).queryByText("First update from the provider.")).toBeNull();
    expect(within(ledger).getByText("Updates and thinking saved")).toBeVisible();
    await user.click(within(ledger).getByRole("button", { name: "View work" }));
    expect(within(ledger).getByText("First update from the provider.")).toBeVisible();
    expect(within(ledger).getByRole("button", { name: "Hide work" })).toHaveAttribute("aria-expanded", "true");
  });

  it("keeps the progress receipt to one action count", () => {
    render(<ActivityLedger compact model={model()} progress={{
      latest: "Collecting reports.",
      receipt: "Updates saved",
      details: <p>Collecting reports.</p>,
    }} />);
    const ledger = screen.getByRole("region", { name: "Work summary" });
    expect(within(ledger).getByText("Updates saved · 2 actions")).toBeVisible();
    expect(within(ledger).queryByText(/activity step/)).toBeNull();
  });

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

  it("updates Now when a newer stream snapshot arrives", () => {
    const { rerender } = render(<ActivityLedger model={model({ currentAction: "Reviewing AirPlay sources." })} />);
    expect(screen.getByText("Reviewing AirPlay sources.")).toBeVisible();

    rerender(<ActivityLedger model={model({ currentAction: "Saving the refreshed results." })} />);
    expect(screen.queryByText("Reviewing AirPlay sources.")).toBeNull();
    expect(screen.getByText("Saving the refreshed results.")).toBeVisible();
  });

  it("loads saved activity only when the audit is expanded", async () => {
    const user = userEvent.setup();
    const onExpandedChange = vi.fn();
    render(<ActivityLedger model={model({ status: "complete", entries: [], currentAction: undefined, actionCount: 0, phases: [] })} onExpandedChange={onExpandedChange} emptyState={<p>Loading saved work…</p>} />);

    expect(onExpandedChange).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Show activity" }));
    expect(onExpandedChange).toHaveBeenCalledWith(true);
    expect(screen.getByText("Loading saved work…")).toBeVisible();
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
    expect(within(attention).getByText("The verification command exited with status 1.", { selector: "small" })).toBeVisible();
    expect(screen.queryByText("Newest first")).toBeNull();
  });
});

it("omits empty completed assistant work but keeps checkpoint controls discoverable", () => {
  const empty = model({status: "complete", entries: [], phases: [], actionCount: 0});
  const { rerender } = render(<ActivityLedger compact model={empty} />);
  expect(screen.queryByRole("region")).toBeNull();
  rerender(<ActivityLedger compact historyPending model={empty} />);
  expect(screen.getByRole("button", {name: "Inspect saved work"})).toBeVisible();
  rerender(<ActivityLedger compact model={{...empty, entries: [{...model().entries[0], countsAsAction: false, status: "complete", kind: "checkpoint"}]}} />);
  expect(screen.getByRole("button", {name: "Show activity"})).toBeVisible();
});

it("collapses failed command details without hiding their status, and preserves expansion during updates", async () => {
  const user = userEvent.setup();
  const failed = model({ status: "attention", attentionCount: 1, entries: [{ ...model().entries[0], status: "failed", brief: "Save finding failed — Command timed out", outputs: [{ label: "stderr", content: "Detailed command failure output" }] }] });
  const { rerender } = render(<ActivityLedger compact model={failed} />);
  expect(screen.queryByText("Detailed command failure output")).not.toBeInTheDocument();
  expect(screen.getByText(/1 warning/)).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Show activity"}));
  await user.click(screen.getByText("Save finding"));
  expect(screen.getByText("Detailed command failure output")).toBeVisible();
  rerender(<ActivityLedger compact model={{ ...failed, durationMs: 9000 }} />);
  expect(screen.getByText("Detailed command failure output")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Hide activity"}));
  expect(screen.queryByText("Detailed command failure output")).not.toBeInTheDocument();

});

it("keeps saved thinking discoverable even when a completed turn used no tools", async () => {
  const user = userEvent.setup();
  render(<ActivityLedger compact model={model({ status: "complete", actionCount: 0, entries: [{ ...model().entries[0], kind: "reasoning", status: "complete", countsAsAction: false, label: "Thinking", summary: "Saved thinking episode" }] })} />);
  await user.click(screen.getByRole("button", { name: "Show activity" }));
  await user.click(screen.getByText("Thinking"));
  expect(screen.getByText("Saved thinking episode")).toBeVisible();
});

it("keeps commentary-only completed work behind one disclosure", async () => {
  const commentary = { streams: { commentary: "The full saved update." } } as unknown as HarnessActivityItem;
  render(<ActivityLedger
    compact
    model={model({
      status: "complete",
      currentAction: undefined,
      actionCount: 0,
      entries: [{ ...model().entries[0], kind: "reasoning", status: "complete", countsAsAction: false, label: "Review sources", sourceItem: commentary }],
    })}
    renderEntryDetails={() => <p>The full saved update.</p>}
  />);
  const ledger = screen.getByRole("region", { name: "Work summary" });
  expect(ledger).toHaveTextContent("1 update");
  expect(ledger).not.toHaveTextContent("thinking episode");
  expect(within(ledger).queryByText("The full saved update.")).toBeNull();
  await userEvent.click(within(ledger).getByRole("button", { name: "Show activity" }));
  await userEvent.click(within(ledger).getByText("Review sources"));
  expect(within(ledger).getByText("The full saved update.")).toBeVisible();
});

it("shows one compact running state and a step count", () => {
  render(<ActivityLedger compact model={model({ durationMs: 74_000 })} />);
  const ledger = screen.getByRole("region", { name: "Work summary" });
  expect(within(ledger).getByText("Running", { selector: ".activity-ledger-compact-header strong" })).toBeVisible();
  expect(within(ledger).getByText("1m 14s")).toBeVisible();
  expect(within(ledger).getByText("Saving verified findings.")).toBeVisible();
  expect(ledger).toHaveTextContent("1 activity step");
});

it("keeps assistant technical failures opt-in while exposing requests for attention", async () => {
  const failed = {...model().entries[0], id: "failed", status: "failed" as const, label: "Workspace read", summary: "Missing path argument", brief: "Missing path argument"};
  const approval = {...model().entries[0], id: "approval", status: "attention" as const, label: "Approval required"};
  render(<ActivityLedger compact model={model({attentionCount: 2, entries: [failed, approval]})} />);
  expect(screen.queryByText("Workspace read")).not.toBeInTheDocument();
  expect(screen.queryByText("Missing path argument")).not.toBeInTheDocument();
  expect(screen.getByText("Approval required")).toBeVisible();
  await userEvent.click(screen.getByRole("button", {name: "Show activity"}));
  await userEvent.click(screen.getByText("Workspace read"));
  expect(screen.getByText("Missing path argument")).toBeVisible();
});

it("keeps completed reasoning without text discoverable", async () => {
  render(<ActivityLedger compact model={model({status: "complete", actionCount: 0, entries: [{...model().entries[0], kind: "reasoning", status: "complete", countsAsAction: false, label: "Reasoning", summary: undefined, payload: {reasoning_summary_state: "not_provided"}}]})} />);
  expect(screen.getByText(/1 thinking episode/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", {name: "Show activity"}));
  expect(screen.getByText("Reasoning")).toBeVisible();
});

it("shows the current tool and expands each running call independently", async () => {
  const user = userEvent.setup();
  const first = { ...model().entries[0], id: "ssh-a", label: "Command on host A", sequence: 1 };
  const second = { ...model().entries[0], id: "ssh-b", label: "Command on host B", sequence: 2 };
  render(<ActivityLedger compact model={model({ currentAction: "Command on host B", entries: [second, first] })}
    renderEntryDetails={(entry) => <p>{entry.id} input</p>} />);
  const ledger = screen.getByRole("region", { name: "Work summary" });
  expect(within(ledger).getByText("Command on host B", { selector: ".activity-ledger-compact-current span" })).toBeVisible();
  await user.click(within(ledger).getByRole("button", { name: "Show activity" }));
  const rows = ledger.querySelectorAll(".activity-ledger-audit li details");
  expect(rows).toHaveLength(2);
  await user.click(within(rows[0] as HTMLElement).getByText("Command on host B"));
  expect(rows[0]).toHaveAttribute("open");
  expect(rows[1]).not.toHaveAttribute("open");
  expect(within(rows[0] as HTMLElement).getByText("ssh-b input")).toBeVisible();
  await user.click(within(rows[1] as HTMLElement).getByText("Command on host A"));
  expect(rows[0]).toHaveAttribute("open");
  expect(rows[1]).toHaveAttribute("open");
  await user.click(within(rows[0] as HTMLElement).getByText("Command on host B"));
  expect(rows[0]).not.toHaveAttribute("open");
  expect(rows[1]).toHaveAttribute("open");
});
