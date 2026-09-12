import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { HarnessThinking } from "./HarnessThinking";
import { HarnessCommandHints, isHarnessCommand } from "./HarnessCommandHints";
import { reduceHarnessActivity } from "../pages/harnessActivity";

describe("Harness thinking and commands", () => {
  for (const vendor of ["grok_acp", "codex_app_server"] as const) {
    it(`keeps ${vendor} thinking accessible through streaming, completion and replay`, async () => {
      const user = userEvent.setup();
      const event = { schemaVersion: "nebula.harness-activity/v2" as const, type: "output_delta" as const, vendor, itemId: "thinking-1", itemKind: "reasoning" as const, itemStatus: "streaming" as const, title: "Reasoning", stream: "reasoning_summary" as const, delta: "First line\nSecond line", sequence: 1, payload: {}, artifactIds: [] };
      const live = reduceHarnessActivity([], event, "assistant");
      const { rerender } = render(<HarnessThinking items={live} />);
      const summary = screen.getByText("Thinking…");
      expect(summary.closest("details")).not.toHaveAttribute("open");
      await user.click(summary);
      expect(screen.getByText(/First line/)).toBeVisible();
      const completed = reduceHarnessActivity(live, { ...event, sequence: 2, type: "item_upsert", delta: undefined, itemStatus: "completed", title: "Thinking" }, "assistant");
      rerender(<HarnessThinking items={completed} />);
      expect(screen.getByText("Thinking")).toBeVisible();
      expect(screen.getByText(/Second line/)).toBeVisible();
      rerender(<HarnessThinking items={JSON.parse(JSON.stringify(completed))} />);
      expect(screen.getByText(/First line/)).toBeVisible();
    });
  }

  it("does not render hidden reasoning or empty content", () => {
    render(<HarnessThinking items={[]} />);
    expect(screen.queryByLabelText("Harness thinking")).toBeNull();
  });

  it("discovers commands and inserts syntax without executing", async () => {
    expect(isHarnessCommand(" /usage ")).toBe(true);
    expect(isHarnessCommand("/goals status")).toBe(true);
    expect(isHarnessCommand("/home/operator/project")).toBe(false);
    expect(isHarnessCommand("Explain /goal")).toBe(false);
    const selected: string[] = [];
    const user = userEvent.setup();
    const { rerender } = render(<HarnessCommandHints draft="/" onSelect={(text) => selected.push(text)} />);
    await user.click(screen.getByRole("button", { name: "/goal Set or inspect a goal" }));
    expect(selected).toEqual(["/goal "]);
    rerender(<HarnessCommandHints draft="/u" onSelect={() => {}} />);
    expect(screen.getByRole("button", { name: "/usage Session usage" })).toBeVisible();
    expect(screen.queryByText("/goal")).toBeNull();
    rerender(<HarnessCommandHints draft="/goal do the work" onSelect={() => {}} />);
    expect(screen.queryByLabelText("Harness commands")).toBeNull();
  });
});


it("uses updated session command metadata and removes withdrawn commands", async () => {
  const selected: string[] = [];
  const commands = [{ name: "vendor-check", description: "Check project", hint: "target", source: "native" as const }];
  const { rerender } = render(<HarnessCommandHints draft="/" commands={commands} onSelect={text => selected.push(text)} />);
  const button = screen.getByRole("button", { name: "/vendor-check Check project" });
  expect(button).toHaveAttribute("title", "target");
  await userEvent.setup().click(button);
  expect(selected).toEqual(["/vendor-check "]);
  expect(isHarnessCommand("/vendor-check target")).toBe(true);
  expect(isHarnessCommand("/unknown")).toBe(true);
  rerender(<HarnessCommandHints draft="/vendor" commands={[]} onSelect={() => {}} />);
  expect(screen.queryByRole("button")).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent("Command unavailable");
});
