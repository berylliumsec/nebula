import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { HarnessReasoningDetails } from "./HarnessReasoningDetails";
import type { HarnessActivityItem } from "../pages/harnessActivity";

function item(overrides: Partial<HarnessActivityItem> = {}): HarnessActivityItem {
  return { assistantId: "a", key: "r", kind: "reasoning", type: "item_upsert", vendor: "codex_app_server", title: "Reasoning", sequence: 1, streams: {}, payload: {}, artifactIds: [], ...overrides };
}

it("keeps long completed and earlier streamed summaries selectable", async () => {
  const text = "Long summary. ".repeat(6000) + "FINAL TAIL";
  const { container } = render(<HarnessReasoningDetails item={item({ streams: { reasoning_summary: text }, payload: { reasoning_summary_state: "available", reasoning_streamed_text: "Earlier public text" } })} />);
  expect(container.querySelector("p")?.textContent).toBe(text);
  expect(container.querySelector("p")).toHaveAttribute("tabindex", "0");
  await userEvent.click(screen.getByText("Earlier streamed summary"));
  expect(screen.getByText("Earlier public text")).toBeVisible();
  expect(screen.getByText("Provider-supplied reasoning summary.")).toBeVisible();
});

it("renders commentary instead of the generic durable event description", () => {
  render(<HarnessReasoningDetails item={item({ streams: { commentary: "Public progress update" }, summary: "chat · Harness output delta", title: "Commentary", vendor: "grok_acp" })} />);
  expect(screen.getByText("Public progress update")).toBeVisible();
  expect(screen.queryByText("chat · Harness output delta")).not.toBeInTheDocument();
});

it("explains unavailable, pending, malformed and historically truncated text", () => {
  const { rerender } = render(<HarnessReasoningDetails item={item({ payload: { reasoning_summary_state: "pending" } })} />);
  expect(screen.getByText(/Thinking is in progress/)).toBeVisible();
  rerender(<HarnessReasoningDetails item={item({ payload: { reasoning_summary_state: "not_provided", reasoning_summary_malformed: true } })} />);
  expect(screen.getByText("No thinking summary was provided by the harness.")).toBeVisible();
  expect(screen.getByText(/Some summary content could not be read/)).toBeVisible();
  rerender(<HarnessReasoningDetails item={item({ streams: { reasoning_summary: "Saved…[truncated]" } })} />);
  expect(screen.getByText(/This saved thinking text was shortened/)).toBeVisible();
});
