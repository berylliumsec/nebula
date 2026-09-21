import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { ChatSubagentView } from "../../api/types";
import { ChatSubagentPane } from "./ChatSubagentPane";
import { ChatSubagentRail } from "./ChatSubagentRail";
import { ChatSubagentResultCard } from "./ChatSubagentResultCard";
import { compactTokens, elapsedLabel, subagentSummary } from "./useChatSubagents";

vi.mock("../../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

const decideApproval = vi.fn();
const stopChatSubagent = vi.fn();
const stopAllChatSubagents = vi.fn();
const api = { decideApproval, stopChatSubagent, stopAllChatSubagents } as unknown as ApiClient;

function subagent(overrides: Partial<ChatSubagentView>): ChatSubagentView {
  return {
    id: "sub-1",
    name: "Map documented API routes",
    task: "List every documented route and compare it against the router files.",
    status: "running",
    parentSessionId: "parent",
    parentTurnId: "turn",
    parentBackend: "provider",
    childSessionId: "child-1",
    stepCount: 6,
    recentSteps: [
      { tool: "read_file", detail: "openapi.yaml", status: "complete" },
      { tool: "search_files", detail: "router.", status: "running" },
    ],
    usage: { inputTokens: 4_000, outputTokens: 2_200, totalTokens: 6_200 },
    startedAt: "2026-09-20T10:00:00Z",
    elapsedSeconds: 38,
    result: "",
    ...overrides,
  };
}

const paneProps = {
  api,
  sessionId: "parent",
  onClose: vi.fn(),
  onOpenConversation: vi.fn(),
  onChanged: vi.fn(),
};

describe("the subagents an operator can see and act on", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows what each child is doing without claiming more than Core reported", () => {
    render(<ChatSubagentPane {...paneProps} subagents={[subagent({})]} />);
    expect(screen.getByText("Map documented API routes")).toBeInTheDocument();
    expect(screen.getByText(/Running · step 6 · 38s · 6.2k tokens/)).toBeInTheDocument();
    expect(screen.getByText(/1 of 3 slots active/)).toBeInTheDocument();
    // The inheritance rule is stated, not implied.
    expect(screen.getByText(/cannot start their own subagents/)).toBeInTheDocument();
  });

  it("opens a child that needs a decision and records the operator's answer", async () => {
    const user = userEvent.setup();
    decideApproval.mockResolvedValue({});
    const waiting = subagent({
      status: "waiting_approval",
      approval: { id: "approval-1", status: "pending", tool: "run_command", detail: "npm ls --all --json", rationale: "Wants to run a command in the workspace" },
    });
    render(<ChatSubagentPane {...paneProps} subagents={[waiting]} />);

    // A child waiting on a person is expanded without being asked.
    expect(screen.getByText("Wants to run a command in the workspace")).toBeInTheDocument();
    expect(screen.getByText("npm ls --all --json")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Allow once" }));
    await waitFor(() => expect(decideApproval).toHaveBeenCalledWith("approval-1", { decision: "approve" }));
    expect(paneProps.onChanged).toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(decideApproval).toHaveBeenLastCalledWith("approval-1", { decision: "reject" }));
  });

  it("stops one child or all of them, and says when it could not", async () => {
    const user = userEvent.setup();
    stopChatSubagent.mockRejectedValueOnce(new Error("Core is offline"));
    render(<ChatSubagentPane {...paneProps} subagents={[subagent({})]} />);

    // A running child is collapsed until the operator opens it.
    await user.click(screen.getByRole("button", { name: /Map documented API routes/ }));
    await user.click(screen.getByRole("button", { name: /^Stop$/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Core is offline");

    stopAllChatSubagents.mockResolvedValue([]);
    await user.click(screen.getByRole("button", { name: /Stop all subagents/ }));
    await waitFor(() => expect(stopAllChatSubagents).toHaveBeenCalledWith("parent"));
  });

  it("offers no stop for a child that already finished", () => {
    render(<ChatSubagentPane {...paneProps} subagents={[subagent({ status: "completed", finishedAt: "2026-09-20T10:02:00Z", resultMessageId: "message-9", result: "Found 47 routes." })]} />);
    expect(screen.getByText(/Done · 38s · 6.2k tokens · result posted/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Stop all subagents/ })).not.toBeInTheDocument();
  });

  it("explains delegation before anything has been delegated", () => {
    render(<ChatSubagentPane {...paneProps} subagents={[]} />);
    expect(screen.getByText("Nothing delegated yet")).toBeInTheDocument();
    expect(screen.getByText(/0 of 3 slots active/)).toBeInTheDocument();
  });

  it("carries the counts on the rail and stays out of the way when empty", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    const { container, rerender } = render(<ChatSubagentRail subagents={[]} open={false} onToggle={onToggle} />);
    expect(container).toBeEmptyDOMElement();

    rerender(<ChatSubagentRail
      open={false}
      onToggle={onToggle}
      subagents={[subagent({}), subagent({ id: "sub-2", status: "waiting_approval" }), subagent({ id: "sub-3", status: "completed", finishedAt: "x" })]}
    />);
    const rail = screen.getByRole("status", { name: "Subagents" });
    expect(rail).toHaveTextContent("1 running");
    expect(rail).toHaveTextContent("1 needs approval");
    expect(rail).toHaveTextContent("1 done");
    await user.click(screen.getByRole("button", { name: "Show subagents" }));
    expect(onToggle).toHaveBeenCalledOnce();
  });

  it("shows a finished child's report in the parent's own words", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const long = "Found 1 high-severity issue. ".repeat(20);
    render(<ChatSubagentResultCard
      onOpenConversation={onOpen}
      subagent={subagent({ status: "completed", finishedAt: "x", result: long, elapsedSeconds: 112 })}
    />);
    expect(screen.getByText("Subagent finished")).toBeInTheDocument();
    expect(screen.getByText(/1m 52s/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Show full result" }));
    expect(screen.getByText((text) => text.startsWith(long.slice(0, 60)) && !text.endsWith("…"))).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Open conversation/ }));
    expect(onOpen).toHaveBeenCalledWith("child-1");
  });

  it("names a child that stopped without pretending it succeeded", () => {
    render(<ChatSubagentResultCard
      onOpenConversation={vi.fn()}
      subagent={subagent({ status: "failed", finishedAt: "x", result: "", error: "The provider ended the turn." })}
    />);
    expect(screen.getByText("Subagent stopped")).toBeInTheDocument();
    expect(screen.getByText("The provider ended the turn.")).toBeInTheDocument();
  });

  it("names the provider model a harness chat delegates to", () => {
    render(<ChatSubagentPane
      {...paneProps}
      subagents={[subagent({ parentBackend: "harness", providerProfileId: "openrouter", model: "deepseek/deepseek-v3.2" })]}
      harnessDelegation={{ harnessName: "Codex", providerName: "OpenRouter", model: "deepseek/deepseek-v3.2" }}
    />);
    expect(screen.getByText(/1 of 3 slots active · deepseek\/deepseek-v3.2 · 6.2k tokens/)).toBeInTheDocument();
    // Where the tool outputs go is stated, not implied.
    expect(screen.getByText(/run on OpenRouter · deepseek\/deepseek-v3.2 .* Their tool outputs go to OpenRouter/)).toBeInTheDocument();
  });

  it("explains harness delegation before anything has been delegated", () => {
    render(<ChatSubagentPane {...paneProps} subagents={[]} harnessDelegation={{ harnessName: "Codex", providerName: "OpenRouter", model: "deepseek/deepseek-v3.2" }} />);
    expect(screen.getByText(/Codex can split independent work across parallel children on deepseek\/deepseek-v3.2/)).toBeInTheDocument();
  });

  it("labels a harness child's report with the model that wrote it", () => {
    const { rerender } = render(<ChatSubagentResultCard
      onOpenConversation={vi.fn()}
      subagent={subagent({ status: "completed", finishedAt: "x", result: "Done.", elapsedSeconds: 112, parentBackend: "harness", model: "deepseek/deepseek-v3.2" })}
    />);
    expect(screen.getByText(/deepseek-v3.2 ·/)).toBeInTheDocument();
    // A provider chat's children share its model, so the card does not repeat it.
    rerender(<ChatSubagentResultCard
      onOpenConversation={vi.fn()}
      subagent={subagent({ status: "completed", finishedAt: "x", result: "Done.", model: "model-a" })}
    />);
    expect(screen.queryByText(/model-a/)).not.toBeInTheDocument();
  });

  it("formats the counts the assistant already uses", () => {
    expect(compactTokens(940)).toBe("940");
    expect(compactTokens(6_200)).toBe("6.2k");
    expect(compactTokens(18_300)).toBe("18.3k");
    expect(compactTokens(60_000)).toBe("60k");
    expect(elapsedLabel(38)).toBe("38s");
    expect(elapsedLabel(112)).toBe("1m 52s");
    expect(subagentSummary([])).toEqual([]);
  });
});
