import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ContextMemory, ContextMemoryItem, ContextStatus } from "../api/types";
import { WorkingContextPanel, contextCapacityLabel } from "./WorkingContextPanel";

const item = (text: string): ContextMemoryItem => ({ text, sources: [{ sourceKind: "chat_message", sourceId: "message-1", sequence: 1 }] });

const memory = (overrides: Partial<ContextMemory> = {}): ContextMemory => ({
  summary: "Reviewing exposed services on the lab range.",
  userRequests: [item("Map the exposed services on 10.20.0.0/24."), item("Skip hosts outside the lab VLAN.")],
  currentState: [item("TLS checked on 12 of 17 listeners.")],
  confirmedFacts: [item("10.20.0.14:443 still accepts TLS 1.0.")],
  decisions: [item("Use testssl.sh for the remaining hosts.")],
  constraints: [item("Read-only scanning; no exploitation.")],
  attempts: [item("sslyze against 10.20.0.22 timed out twice.")],
  corrections: [],
  references: [item("/home/op/scans/tls-2026-09-26.json — combined TLS results")],
  openQuestions: [item("Is 10.20.0.40 in scope?")],
  evidenceIds: [],
  artifactIds: [],
  ...overrides,
});

const sources = Array.from({ length: 23 }, (_, index) => ({ sourceKind: "chat_message", sourceId: `message-${index}`, sequence: index + 1 }));

function contextStatus(overrides: Partial<ContextStatus> = {}): ContextStatus {
  return {
    ownerType: "chat_session",
    ownerId: "session-1",
    status: "ready",
    contextWindow: 128_000,
    maxOutputTokens: 8_000,
    targetInputTokens: 91_500,
    capacitySource: "model_catalog",
    routeLimitsRequired: false,
    routeLimitsVerified: false,
    bindingLimit: "model",
    inputCapacity: 120_000,
    inputLimitBinds: false,
    estimatedInputTokens: 71_240,
    estimateCalibration: 1.12,
    lastProviderRequest: {
      instructions: 4_000, conversation: 60_000, toolSchemas: 5_000, toolResults: 0, other: 0,
      estimatedTotal: 69_000, reportedInputTokens: 68_912, reportedCachedInputTokens: 42_036, attempt: 1,
    },
    compactedThrough: 42,
    sourceReferences: sources,
    compactionUsage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 },
    compactionCostUsd: 0,
    snapshot: {
      id: "snapshot-1", ownerType: "chat_session", ownerId: "session-1", version: 3, status: "ready",
      compactedThrough: 42, memory: memory(), sourceReferences: sources, providerId: "provider-1", model: "model-1",
      promptVersion: "nebula-context-v2", usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 }, costUsd: 0,
      quality: "complete", droppedItems: 0, createdAt: "2026-09-26T10:00:00Z",
    },
    quality: "complete",
    workingNotes: {
      content: "## Findings\n\n- 10.20.0.14:443 accepts TLS 1.0\n\n## Todo\n\n- [x] Sweep 10.20.0.0/24\n- [ ] Ask whether .40 is in scope",
      revision: 6,
      updatedAt: new Date(Date.now() - 2 * 60_000).toISOString(),
      turnId: "turn-9",
    },
    ...overrides,
  };
}

function renderPanel(status?: ContextStatus, props: Partial<Parameters<typeof WorkingContextPanel>[0]> = {}) {
  return render(<WorkingContextPanel hasSession loading={false} status={status} onRetry={() => undefined} {...props} />);
}

describe("WorkingContextPanel", () => {
  it("summarises a calibrated meter, saved memory, agent notes and the last request while collapsed", () => {
    renderPanel(contextStatus());

    expect(screen.getByRole("heading", { name: "Working context" })).toBeVisible();
    expect(screen.getByText("71,240 of 91,500 target input tokens · calibrated from provider usage · exact model catalog")).toBeVisible();
    expect(screen.getByRole("meter", { name: "78 percent of target input used" })).toHaveAttribute("aria-valuenow", "78");
    expect(screen.getByText("Compacted through message 42. The original messages are unchanged and searchable.")).toBeVisible();
    expect(screen.getByText("The meter estimates the next request: conversation, instructions and tool definitions, corrected by the input tokens the provider reported last time.")).toBeVisible();

    const memoryDisclosure = screen.getByText("Inspect saved memory").closest("details")!;
    expect(memoryDisclosure).not.toHaveAttribute("open");
    expect(within(memoryDisclosure).getByText("· 9 sections, 23 sources", { exact: false })).toBeInTheDocument();
    const notesDisclosure = screen.getByText("Agent notes").closest("details")!;
    expect(notesDisclosure).not.toHaveAttribute("open");
    expect(notesDisclosure.querySelector("summary")).toHaveTextContent("Agent notes · updated 2 min ago");
    expect(screen.getByText("Last provider request").closest("summary")).toHaveTextContent("Last provider request · 68,912 reported · 61% cached");
    // Nothing reads as degraded when the memory is complete.
    expect(screen.queryByText(/partial summary/)).not.toBeInTheDocument();
    expect(document.querySelector(".session-context-summary .status-dot")).toHaveClass("healthy");
  });

  it("lists every non-empty v2 memory section in the compactor's order", async () => {
    renderPanel(contextStatus());
    await userEvent.click(screen.getByText("Inspect saved memory"));

    const body = screen.getByText("Inspect saved memory").closest("details")!.querySelector(":scope > div")!;
    const headings = Array.from(body.querySelectorAll("section > strong")).map((node) => node.textContent);
    expect(headings).toEqual([
      "Summary", "Operator requests", "Current state and next steps", "Decisions", "Constraints",
      "Confirmed facts", "Attempts", "References", "Open questions",
    ]);
    // Corrections is empty in this memory, so it stays hidden.
    expect(within(body as HTMLElement).queryByText("Corrections")).not.toBeInTheDocument();
    expect(within(body as HTMLElement).getByText("Map the exposed services on 10.20.0.0/24.")).toBeVisible();
    expect(within(body as HTMLElement).getByText("/home/op/scans/tls-2026-09-26.json — combined TLS results")).toBeVisible();
    expect(within(body as HTMLElement).getByText("23 source references · private reasoning is not stored")).toBeVisible();
  });

  it("explains a partial summary in place without hiding the chat's usable state", async () => {
    renderPanel(contextStatus({
      quality: "degraded",
      snapshot: { ...contextStatus().snapshot!, quality: "degraded", memory: memory({ summary: "Automatic summarisation failed.", decisions: [], constraints: [] }) },
    }));

    const status = document.querySelector(".session-context-summary")!;
    expect(status.querySelector("strong")).toHaveTextContent("ready · partial summary");
    expect(status.querySelector(".status-dot")).toHaveClass("partial");
    expect(within(status as HTMLElement).getByText(/The automatic summary failed, so Nebula kept every operator request and exact identifier from the older messages\. The assistant can still search the originals\. The summary is rebuilt at the next compaction\./)).toBeVisible();
    expect(screen.getByText("Inspect saved memory").closest("summary")).toHaveTextContent("Inspect saved memory · requests and identifiers only");
    // The meter keeps measuring while the summary is partial.
    expect(screen.getByLabelText("78 percent of target input used")).toBeVisible();

    await userEvent.click(screen.getByText("Agent notes"));
    const notes = screen.getByText("Agent notes").closest("details")!;
    expect(notes).toHaveAttribute("open");
    // Markdown, not raw text: headings and task-list checkboxes render.
    expect(within(notes).getByRole("heading", { name: "Findings" })).toBeVisible();
    expect(within(notes).getAllByRole("checkbox")).toHaveLength(2);
    expect(within(notes).getByText("Kept by the assistant for this conversation · revision 6 · read-only here")).toBeVisible();
    expect(within(notes).queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("counts items removed by validation on the status line", () => {
    renderPanel(contextStatus({ quality: "salvaged", snapshot: { ...contextStatus().snapshot!, quality: "salvaged", droppedItems: 3 } }));
    expect(document.querySelector(".session-context-summary strong")).toHaveTextContent("ready · 3 items removed");
    expect(document.querySelector(".session-context-summary .status-dot")).toHaveClass("partial");
    // A salvaged memory is still a model summary, so the token line stays.
    expect(screen.getByText(/71,240 of 91,500 target input tokens/)).toBeVisible();
    expect(screen.getByText("Inspect saved memory").closest("summary")).toHaveTextContent("· 9 sections, 23 sources");
  });

  it("renders an older Core's status without claiming calibration, cache hits or notes", () => {
    const { snapshot, ...rest } = contextStatus();
    renderPanel({
      ...rest,
      estimateCalibration: undefined,
      quality: undefined,
      workingNotes: undefined,
      lastProviderRequest: { ...rest.lastProviderRequest!, reportedInputTokens: 18, reportedCachedInputTokens: undefined },
      snapshot: {
        ...snapshot!,
        quality: "complete",
        memory: memory({ userRequests: [], currentState: [], attempts: [], references: [], constraints: [], openQuestions: [] }),
      },
    });

    expect(screen.getByText("71,240 of 91,500 target input tokens · exact model catalog")).toBeVisible();
    expect(screen.queryByText(/calibrated/)).not.toBeInTheDocument();
    expect(screen.getByText("The meter estimates the next request: conversation, instructions and tool definitions.")).toBeVisible();
    expect(screen.queryByText("Agent notes")).not.toBeInTheDocument();
    expect(screen.getByText("Last provider request").closest("summary")).toHaveTextContent(/^Last provider request · 18 reported$/);
    expect(screen.getByText("Inspect saved memory").closest("summary")).toHaveTextContent("· 3 sections, 23 sources");
  });

  it("keeps the harness, loading, failure and first-turn states", async () => {
    const retry = vi.fn();
    const { rerender } = renderPanel(undefined, { hasSession: false });
    expect(screen.getByText("Context becomes durable after the first saved turn.")).toBeVisible();

    rerender(<WorkingContextPanel hasSession loading status={undefined} onRetry={retry} />);
    expect(screen.getByText("Reading Core context…")).toBeVisible();

    rerender(<WorkingContextPanel hasSession loading={false} error="Core is unreachable." status={undefined} onRetry={retry} />);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledOnce();

    rerender(<WorkingContextPanel hasSession loading={false} status={contextStatus({ status: "runtime_managed", snapshot: undefined, workingNotes: undefined })} onRetry={retry} />);
    expect(screen.getByText("Harness managed")).toBeVisible();
    expect(screen.getByText("The selected harness owns compaction and reports its usage through activity.")).toBeVisible();
    expect(screen.queryByText(/The meter estimates/)).not.toBeInTheDocument();
    expect(screen.queryByRole("meter")).not.toBeInTheDocument();
  });
});

describe("contextCapacityLabel", () => {
  // OpenRouter with verified routes, as Core reports it for one conversation.
  const routed = (overrides: Partial<ContextStatus>): ContextStatus => contextStatus({
    routeLimitsRequired: true, routeLimitsVerified: true, eligibleRouteCount: 27, ...overrides,
  });

  it("leads with the configured cap Core says binds, and keeps the route facts after it", () => {
    // options.context_window 16,000 under routes that accept 1,000,000.
    expect(contextCapacityLabel(routed({
      contextWindow: 16_000, maxOutputTokens: 2_000, targetInputTokens: 10_500, bindingLimit: "configured",
      inputCapacity: 14_000, routeContextWindow: 1_000_000, routeInputLimit: 1_000_000,
    }))).toBe("16,000 configured cap · 14,000 input ceiling · 27 compatible routes · 1,000,000 route minimum");
  });

  it("leads with the routes when the smallest route binds, with Core's input ceiling", () => {
    expect(contextCapacityLabel(routed({
      contextWindow: 65_536, bindingLimit: "route", inputCapacity: 57_000, inputLimitBinds: true,
      routeContextWindow: 65_536, routeInputLimit: 57_000,
    }))).toBe("27 compatible routes · 65,536 route minimum · 57,000 input ceiling");
    // Core reports the ceiling; the label no longer re-derives it from the window.
    expect(contextCapacityLabel(routed({
      contextWindow: 65_536, bindingLimit: "route", inputCapacity: 30_000, inputLimitBinds: true,
      routeContextWindow: 65_536, routeInputLimit: 65_536,
    }))).toBe("27 compatible routes · 65,536 route minimum · 30,000 input ceiling");
  });

  it("names a model window below every route", () => {
    expect(contextCapacityLabel(routed({
      contextWindow: 128_000, bindingLimit: "model", inputCapacity: 120_000, routeContextWindow: 200_000,
    }))).toBe("128,000 model window · 120,000 input ceiling · 27 compatible routes · 200,000 route minimum");
  });

  it("does not infer a configured cap from an older Core that omits the binding limit", () => {
    // The window is below the route minimum, but nothing says a configured window set it.
    expect(contextCapacityLabel(routed({
      contextWindow: 16_000, bindingLimit: undefined, inputCapacity: undefined, routeContextWindow: 1_000_000,
    }))).toBe("27 compatible routes · 1,000,000 route minimum");
  });

  it("leads with a configured cap outside verified routes too", () => {
    // A 16,000 configured window under a model catalog that allows more.
    expect(contextCapacityLabel(contextStatus({ contextWindow: 16_000, bindingLimit: "configured" })))
      .toBe("16,000 configured cap · exact model catalog");
    expect(contextCapacityLabel(contextStatus({ contextWindow: 16_000, bindingLimit: "configured", capacitySource: "known_model" })))
      .toBe("16,000 configured cap · published model limits");
    // With no model window the configured value is the estimate itself.
    expect(contextCapacityLabel(contextStatus({ contextWindow: 16_000, bindingLimit: "configured", capacitySource: "configured" })))
      .toBe("configured estimate");
    expect(contextCapacityLabel(contextStatus({ bindingLimit: "model" }))).toBe("exact model catalog");
    // Unverified OpenRouter routes: a configured window below the safe ceiling leads.
    expect(contextCapacityLabel(contextStatus({ routeLimitsRequired: true, contextWindow: 6_000, bindingLimit: "configured" })))
      .toBe("6,000 configured cap · route limits unverified");
    expect(contextCapacityLabel(contextStatus({ routeLimitsRequired: true, contextWindow: 8_192, bindingLimit: "fallback" })))
      .toBe("route limits unverified · safe 8,192-token ceiling");
  });
});
