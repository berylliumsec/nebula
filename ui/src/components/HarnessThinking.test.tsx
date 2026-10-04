import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { HarnessActivityItem } from "../pages/harnessActivity";
import { HarnessThinking, ThinkingDisclosure } from "./HarnessThinking";

describe("ThinkingDisclosure", () => {
  it("hides model thoughts until the operator expands them", async () => {
    render(<ThinkingDisclosure text="Private chain of thought." />);
    const disclosure = screen.getByLabelText("Thinking");
    expect(disclosure).toBeInTheDocument();
    expect(disclosure).not.toHaveAttribute("open");
    expect(screen.queryByText("Private chain of thought.")).not.toBeInTheDocument();
    await userEvent.click(disclosure.querySelector("summary")!);
    expect(disclosure).toHaveAttribute("open");
    expect(screen.getByText("Private chain of thought.")).toBeVisible();
  });

  it("mounts harness summaries only after expansion", async () => {
    render(<HarnessThinking items={[{
      assistantId: "assistant-1",
      key: "thought-1",
      type: "reasoning",
      kind: "reasoning",
      vendor: "codex_app_server",
      status: "completed",
      title: "Reasoning",
      sequence: 1,
      streams: { reasoning_summary: "Deferred harness summary." },
      payload: {},
      artifactIds: [],
    }]} />);

    expect(screen.queryByText("Deferred harness summary.")).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Harness thinking").querySelector("summary")!);
    expect(screen.getByText("Deferred harness summary.")).toBeVisible();
  });

  it("keeps Thinking after Codex completes an episode without a public summary", async () => {
    const episode: HarnessActivityItem = {
      assistantId: "assistant-1",
      key: "reasoning-1",
      type: "item_upsert",
      kind: "reasoning",
      vendor: "codex_app_server",
      status: "running",
      title: "Reasoning",
      sequence: 1,
      streams: {},
      payload: { reasoning_summary_state: "pending" },
      artifactIds: [],
    };
    const { rerender } = render(<HarnessThinking items={[episode]} />);
    const thinking = screen.getByLabelText("Harness thinking");
    expect(thinking).toHaveTextContent("Thinking…");

    rerender(<HarnessThinking items={[{ ...episode, status: "completed", sequence: 2, payload: { reasoning_summary_state: "not_provided" } }]} />);
    expect(thinking).toBeInTheDocument();
    expect(thinking.querySelector("summary")).toHaveTextContent("Thinking · 1 update");
    expect(thinking.querySelector("summary")).toHaveTextContent("No public summaries for completed updates");
    await userEvent.click(thinking.querySelector("summary")!);
    expect(screen.getByText("Codex did not provide a public summary for one reasoning episode.")).toBeVisible();
    expect(thinking.querySelector(".harness-thinking-list")).toBeNull();
  });

  it("counts reasoning episodes while stating that their public summaries are missing", async () => {
    const items: HarnessActivityItem[] = Array.from({ length: 349 }, (_, index) => ({
      assistantId: "assistant-1",
      key: `reasoning-${index}`,
      type: "item_upsert",
      kind: "reasoning",
      vendor: "codex_app_server",
      status: "completed",
      title: "Reasoning",
      sequence: index,
      streams: {},
      payload: { reasoning_summary_state: "not_provided" },
      artifactIds: [],
    }));
    render(<HarnessThinking items={items} />);
    const thinking = screen.getByLabelText("Harness thinking");
    expect(thinking.querySelector("summary")).toHaveTextContent("Thinking · 349 updates");
    expect(thinking.querySelector("summary")).toHaveTextContent("No public summaries for completed updates");
    await userEvent.click(thinking.querySelector("summary")!);
    expect(screen.getByText("Codex did not provide public summaries for 349 reasoning episodes.")).toBeVisible();
  });

  it("does not render when the model returned no thoughts", () => {
    const { container } = render(<ThinkingDisclosure text="" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("loads saved thoughts only after expansion", async () => {
    const load = vi.fn(async () => "Saved reasoning from Core.");
    render(<ThinkingDisclosure load={load} />);
    expect(load).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("Thinking"));
    expect(await screen.findByText("Saved reasoning from Core.")).toBeVisible();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("retries a failed saved-thought read in place", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("Core is reconnecting"))
      .mockResolvedValueOnce("Recovered reasoning.");
    render(<ThinkingDisclosure load={load} />);
    await userEvent.click(screen.getByText("Thinking"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Core is reconnecting");
    await userEvent.click(screen.getByRole("button", {name: "Retry"}));
    expect(await screen.findByText("Recovered reasoning.")).toBeVisible();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("keeps model paragraph boundaries semantic inside the disclosure", async () => {
    const { container } = render(
      <ThinkingDisclosure text={"First reasoning paragraph.\n\nSecond reasoning paragraph."} />,
    );

    await userEvent.click(screen.getByText("Thinking"));

    const paragraphs = container.querySelectorAll(".assistant-markdown p");
    expect(paragraphs).toHaveLength(2);
    expect(paragraphs[0]).toHaveTextContent("First reasoning paragraph.");
    expect(paragraphs[1]).toHaveTextContent("Second reasoning paragraph.");
  });

  it("shows a bounded recent history before loading every thinking update", async () => {
    const items: HarnessActivityItem[] = Array.from({ length: 12 }, (_, index) => ({
      assistantId: "assistant-1",
      key: `thought-${index}`,
      type: "reasoning",
      kind: "reasoning",
      vendor: "codex_app_server" as const,
      status: "completed",
      title: "Reasoning",
      sequence: index,
      streams: { reasoning_summary: `Checking step ${index + 1}` },
      payload: {},
      artifactIds: [],
    }));
    render(<HarnessThinking items={items} />);

    await userEvent.click(screen.getByLabelText("Harness thinking").querySelector("summary")!);
    expect(screen.getByText("Latest 8 of 12")).toBeVisible();
    expect(screen.queryByText("Checking step 1")).not.toBeInTheDocument();
    expect(screen.getByText("Checking step 12")).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Show all 12" }));
    expect(screen.getByText("Checking step 1")).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(12);
  });
});
