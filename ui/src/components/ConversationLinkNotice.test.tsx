import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { EngagementSummary } from "../api/types";
import { ConversationLinkNotice } from "./ConversationLinkNotice";

const handlers = () => ({ onNewChat: vi.fn(), onRetry: vi.fn(), onRestoreProject: vi.fn() });

describe("ConversationLinkNotice", () => {
  it("announces the lookup without offering a composer", () => {
    render(<ConversationLinkNotice state={{ sessionId: "chat", status: "resolving" }} {...handlers()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Opening conversation…");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("explains a deleted conversation and offers a new chat", async () => {
    const actions = handlers();
    render(<ConversationLinkNotice state={{ sessionId: "chat", status: "missing" }} {...actions} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Conversation not found");
    await userEvent.click(screen.getByRole("button", { name: "Start new chat" }));
    expect(actions.onNewChat).toHaveBeenCalledOnce();
  });

  it("retries a failed lookup in place", async () => {
    const actions = handlers();
    render(<ConversationLinkNotice state={{ sessionId: "chat", status: "failed", message: "Core is restarting." }} {...actions} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Core is restarting.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(actions.onRetry).toHaveBeenCalledOnce();
  });

  it("restores the archived project that owns the conversation", async () => {
    const actions = handlers();
    const project = { id: "old", name: "Old project", status: "archived" } as EngagementSummary;
    const { rerender } = render(<ConversationLinkNotice state={{ sessionId: "chat", status: "archived", project }} {...actions} />);
    expect(screen.getByRole("alert")).toHaveTextContent("It belongs to Old project.");
    await userEvent.click(screen.getByRole("button", { name: "Restore project" }));
    expect(actions.onRestoreProject).toHaveBeenCalledWith("old");

    rerender(<ConversationLinkNotice state={{ sessionId: "chat", status: "archived", project, restoreError: "Core is offline." }} restoring={false} {...actions} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Old project could not be restored. Core is offline. Try again.");
    rerender(<ConversationLinkNotice state={{ sessionId: "chat", status: "archived", project }} restoring {...actions} />);
    expect(screen.getByRole("button", { name: "Restoring…" })).toBeDisabled();
  });
});
