import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ChatRecordedContext } from "./ChatRecordedContext";

function page(sequence: number, nextOffset: number | null) {
  return { items: [{ operator_decisions: [], message_id: `message-${sequence}`, sequence, attachments: [] }], next_offset: nextOffset };
}

describe("ChatRecordedContext", () => {
  it("offers a way back to newer context after paging to older turns", async () => {
    const user = userEvent.setup();
    const request = vi.fn(async (path: string) => path.endsWith("offset=0") ? page(1, 1) : page(2, null));
    render(<ChatRecordedContext api={{ request } as unknown as ApiClient} sessionId="session-1" onMessage={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "Message 1" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Newer context" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Older context" }));
    expect(await screen.findByRole("button", { name: "Message 2" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Message 1" })).toBeNull();
    expect(request).toHaveBeenLastCalledWith("chat/sessions/session-1/context-sources?offset=1", expect.anything());

    await user.click(screen.getByRole("button", { name: "Newer context" }));
    expect(await screen.findByRole("button", { name: "Message 1" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Message 2" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Newer context" })).toBeNull();
    expect(screen.getByRole("button", { name: "Older context" })).toBeVisible();
  });
});
