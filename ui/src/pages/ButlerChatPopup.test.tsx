import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { PersistedChatMessage } from "../api/types";
import { ButlerChatPopup } from "./ButlerChatPopup";

vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

function message(sequence: number, role: "user" | "assistant", content: string, replacedAt?: string) {
  return { id: `m${sequence}`, sequence, role, content, replacedAt } as unknown as PersistedChatMessage;
}

describe("Butler mode chat popup", () => {
  it("pages history with replaced messages included and shows only the live transcript", async () => {
    // 60 rows fill a page, so "Show earlier messages" stays offered.
    const latest = [
      message(1, "user", "Original question", "2026-11-20T18:00:00Z"),
      message(2, "assistant", "Answer to the replaced question", "2026-11-20T18:00:00Z"),
      ...Array.from({ length: 58 }, (_, index) => message(index + 3, index % 2 ? "assistant" : "user", `Message ${index + 3}`)),
    ];
    const older = [message(0, "user", "The very first question")];
    const listChatMessages = vi.fn(async (_id: string, _signal?: AbortSignal, options?: { beforeMessageId?: string }) => (options?.beforeMessageId ? older : latest));
    const api = { listChatMessages } as unknown as ApiClient;
    const user = userEvent.setup();
    render(<ButlerChatPopup api={api} sessionId="chat-1" title="Simulation" live={false} onClose={vi.fn()} onOpenFull={vi.fn()} />);

    expect(await screen.findByText("Message 3")).toBeInTheDocument();
    // Core refuses a paged read without replaced messages (422).
    expect(listChatMessages).toHaveBeenCalledWith("chat-1", expect.anything(), { includeReplaced: true, limit: 60 });
    expect(screen.queryByText("Original question")).not.toBeInTheDocument();
    expect(screen.queryByText("Answer to the replaced question")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Show earlier messages" }));
    expect(await screen.findByText("The very first question")).toBeInTheDocument();
    // Older pages start before the oldest stored row, replaced or not.
    await waitFor(() => expect(listChatMessages).toHaveBeenLastCalledWith("chat-1", undefined, { includeReplaced: true, limit: 60, beforeMessageId: "m1" }));
  });

  it("sizes its layout row to the viewport, not to a long transcript", async () => {
    // An implicit auto row grew to the transcript's full height and centred
    // the popup far below the viewport; only the scrim stayed on screen.
    const long = Array.from({ length: 60 }, (_, index) => message(index + 1, index % 2 ? "assistant" : "user", `Message ${index + 1} ${"long line ".repeat(100)}`));
    const api = { listChatMessages: vi.fn(async () => long) } as unknown as ApiClient;
    render(<ButlerChatPopup api={api} sessionId="chat-1" title="Long chat" live={false} onClose={vi.fn()} onOpenFull={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Chat: Long chat" });
    expect(await screen.findByText(/^Message 60 /)).toBeInTheDocument();
    expect(getComputedStyle(dialog.parentElement!).gridTemplateRows).toBe("minmax(0, 1fr)");
  });
});
