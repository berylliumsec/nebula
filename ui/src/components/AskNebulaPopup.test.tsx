import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { AskNebulaPopup } from "./AskNebulaPopup";
import { createSelectionDraft } from "./selection";

const context = createSelectionDraft({ text: "Selected bytes", source: { kind: "conversation", id: "main", label: "Selected response" } })!;
const snapshot = { engagementId: "project", sessionId: "main", providerId: "provider", model: "m" };
function fixture() {
  const api = {
    createTemporaryChat: vi.fn().mockResolvedValue({ id: "popup", backend: "provider", providerId: "provider", model: "m" }),
    discardTemporaryChat: vi.fn().mockResolvedValue(undefined),
    cancelChatTurn: vi.fn().mockResolvedValue({}),
    streamChat: vi.fn().mockResolvedValue({ message: { role: "assistant", content: "A separate answer" } }),
  };
  return api;
}

describe("Ask Nebula popup", () => {
  it("forks once, keeps follow-ups in the popup, and discards on close", async () => {
    const api = fixture(); const user = userEvent.setup();
    const { unmount } = render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
    await waitFor(() => expect(api.createTemporaryChat).toHaveBeenCalledWith(snapshot));
    await user.type(screen.getByRole("textbox"), "Explain this");
    await user.click(screen.getByRole("button", { name: "Ask question" }));
    expect(await screen.findByText("A separate answer")).toBeVisible();
    expect(api.streamChat.mock.calls[0][0]).toMatchObject({ sessionId: "popup", toolsEnabled: false, contextAttachments: [{ text: "Selected bytes" }] });
    await user.type(screen.getByRole("textbox"), "Why?");
    await user.click(screen.getByRole("button", { name: "Ask question" }));
    expect(api.createTemporaryChat).toHaveBeenCalledTimes(1);
    expect(api.streamChat.mock.calls[1][0].sessionId).toBe("popup");
    unmount();
    expect(api.discardTemporaryChat).toHaveBeenCalledWith("popup");
  });

  it("discards a branch that finishes opening after the popup closes", async () => {
    const api = fixture(); let resolve!: (session: unknown) => void;
    api.createTemporaryChat.mockReturnValue(new Promise(done => { resolve = done; }));
    const { unmount } = render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
    unmount();
    await act(async () => resolve({ id: "late" }));
    expect(api.discardTemporaryChat).toHaveBeenCalledWith("late");
  });

  it("keeps failed questions editable and retries in the same branch", async () => {
    const api = fixture(); const user = userEvent.setup();
    api.streamChat.mockRejectedValueOnce(new Error("Connection lost. Try again."));
    render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
    await user.type(screen.getByRole("textbox"), "Question");
    await user.click(screen.getByRole("button", { name: "Ask question" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection lost");
    expect(screen.getByRole("textbox")).toHaveValue("Question");
    await user.click(screen.getByRole("button", { name: "Ask question" }));
    expect(await screen.findByText("A separate answer")).toBeVisible();
    expect(api.createTemporaryChat).toHaveBeenCalledTimes(1);
  });
});

it("stops only the popup turn and keeps the question editable", async () => {
  const api = fixture(); const user = userEvent.setup();
  api.streamChat.mockImplementation((_body, onEvent, signal) => new Promise((_resolve, reject) => {
    onEvent({ type: "started", turnId: "popup-turn", model: "m", sessionId: "popup" });
    signal.addEventListener("abort", () => reject(new DOMException("Stopped", "AbortError")));
  }));
  render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
  await user.type(screen.getByRole("textbox"), "Explain slowly");
  await user.click(screen.getByRole("button", { name: "Ask question" }));
  await user.click(await screen.findByRole("button", { name: "Stop response" }));
  expect(api.cancelChatTurn).toHaveBeenCalledExactlyOnceWith("popup-turn");
  await expect(screen.getByRole("textbox")).toBeEnabled();
  expect(screen.getByRole("textbox")).toHaveValue("Explain slowly");
});


it("leaves the page interactive and supports keyboard repositioning without dismissal", async () => {
  const api = fixture(); const user = userEvent.setup(); const close = vi.fn();
  render(<><button>Underlying page action</button><AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={close} /></>);
  const popup = screen.getByRole("dialog", { name: "Ask Nebula" });
  expect(popup).not.toHaveAttribute("aria-modal", "true");
  await user.click(screen.getByRole("button", { name: "Underlying page action" }));
  expect(screen.getByRole("button", { name: "Underlying page action" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(close).not.toHaveBeenCalled();
  const top = Number.parseFloat(popup.style.top);
  screen.getByRole("button", { name: "Move Ask Nebula" }).focus();
  await user.keyboard("{ArrowDown}");
  expect(Number.parseFloat(popup.style.top)).toBe(top + 24);
  await user.click(screen.getByRole("button", { name: "Close Ask Nebula" }));
  expect(close).toHaveBeenCalledOnce();
});
