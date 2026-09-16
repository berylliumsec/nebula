import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { ChatStreamEvent } from "../api/types";
import { AskNebulaPopup } from "./AskNebulaPopup";
import { createSelectionDraft } from "./selection";

const context = createSelectionDraft({ text: "Selected bytes", source: { kind: "conversation", id: "main", label: "Selected response" } })!;
const snapshot = { engagementId: "project", sessionId: "main", providerId: "provider", model: "m" };
function fixture() {
  const api = {
    createTemporaryChat: vi.fn().mockResolvedValue({ id: "popup", backend: "provider", providerId: "provider", model: "m" }),
    discardTemporaryChat: vi.fn().mockResolvedValue(undefined),
    keepTemporaryChatAlive: vi.fn().mockResolvedValue(undefined),
    cancelChatTurn: vi.fn().mockResolvedValue({}),
    stopHarnessTurn: vi.fn().mockResolvedValue(undefined),
    getPendingChatTurn: vi.fn().mockResolvedValue({ id: "accepted-turn" }),
    streamChat: vi.fn().mockResolvedValue({ message: { role: "assistant", content: "A separate answer" } }),
  };
  return api;
}

describe("Ask Nebula popup", () => {
  it("recovers when temporary conversation opening stalls and discards a late branch", async () => {
    vi.useFakeTimers();
    try {
      const api = fixture();
      let finishFirst!: (value: unknown) => void;
      let finishRetry!: (value: unknown) => void;
      api.createTemporaryChat
        .mockReturnValueOnce(new Promise(resolve => { finishFirst = resolve; }))
        .mockReturnValueOnce(new Promise(resolve => { finishRetry = resolve; }));
      render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(screen.getByRole("alert")).toHaveTextContent("taking too long");
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      expect(api.createTemporaryChat).toHaveBeenCalledTimes(2);
      await act(async () => finishFirst({ id: "late", backend: "provider", providerId: "provider", model: "m" }));
      expect(api.discardTemporaryChat).toHaveBeenCalledWith("late");
      await act(async () => finishRetry({ id: "recovered", backend: "provider", providerId: "provider", model: "m" }));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Ask question" })).toBeDisabled();
      fireEvent.input(screen.getByRole("textbox", { name: "Question for Nebula" }), { target: { value: "Explain the selection" } });
      expect(screen.getByRole("button", { name: "Ask question" })).toBeEnabled();
    } finally { vi.useRealTimers(); }
  });

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

  it("keeps the streamed agent result when the completion snapshot has no text", async () => {
    const api = fixture(); const user = userEvent.setup();
    api.streamChat.mockImplementation(async (_body, onEvent) => {
      onEvent({ type: "message_delta", providerId: "provider", model: "m", delta: "**Agent result**\n\n- first finding" });
      return { message: { role: "assistant", content: "" } };
    });
    render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
    await user.type(screen.getByRole("textbox"), "Run the agent");
    await user.click(screen.getByRole("button", { name: "Ask question" }));
    expect(await screen.findByText("Agent result")).toHaveRole("strong");
    expect(screen.getByRole("listitem")).toHaveTextContent("first finding");
    expect(screen.queryByText("completed without a text result")).not.toBeInTheDocument();
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

it("moves the hidden launcher by keyboard while keeping Show separate", async () => {
  const api = fixture(); const user = userEvent.setup(); const onVisibilityChange = vi.fn();
  render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} onVisibilityChange={onVisibilityChange} />);
  await user.click(screen.getByRole("button", { name: "Hide Ask Nebula" }));
  expect(onVisibilityChange).toHaveBeenLastCalledWith(false);
  const moveButton = screen.getByRole("button", { name: "Move hidden Ask Nebula" });
  const launcher = moveButton.parentElement!;
  moveButton.focus();
  await user.keyboard("{ArrowLeft}");
  expect(launcher.style.left).not.toBe("");
  expect(screen.getByRole("button", { name: /Show Ask Nebula/ })).toBeVisible();
  await user.click(screen.getByRole("button", { name: /Show Ask Nebula/ }));
  expect(screen.getByRole("dialog", { name: "Ask Nebula" })).toBeVisible();
  expect(onVisibilityChange).toHaveBeenLastCalledWith(true);
});


it("shows harness progress and stops using its harness turn identity before any answer", async () => {
  const api = fixture(); const user = userEvent.setup();
  api.streamChat.mockImplementation((_body, onEvent, signal) => new Promise((_resolve, reject) => {
    onEvent({ type: "status", phase: "running", detail: "Preparing context", harnessTurnId: "harness-only" });
    onEvent({ type: "output_delta", stream: "commentary", delta: "Checking the selected context.", harnessTurnId: "harness-only" });
    signal.addEventListener("abort", () => reject(new DOMException("Stopped", "AbortError")));
  }));
  render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
  await user.type(screen.getByRole("textbox"), "Explain");
  await user.click(screen.getByRole("button", { name: "Ask question" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Checking the selected context.");
  await user.click(screen.getByRole("button", { name: "Stop response" }));
  expect(api.stopHarnessTurn).toHaveBeenCalledExactlyOnceWith("harness-only");
  expect(api.cancelChatTurn).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox")).toBeEnabled();
  expect(screen.getByRole("textbox")).toHaveValue("Explain");
});

it("finds the accepted popup turn when its first stream event has not arrived", async () => {
  const api = fixture(); const user = userEvent.setup();
  api.streamChat.mockImplementation((_body, _onEvent, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("Stopped", "AbortError")));
  }));
  render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
  await user.type(screen.getByRole("textbox"), "Waiting");
  await user.click(screen.getByRole("button", { name: "Ask question" }));
  await user.click(screen.getByRole("button", { name: "Stop response" }));
  expect(api.getPendingChatTurn).toHaveBeenCalledExactlyOnceWith("popup");
  expect(api.cancelChatTurn).toHaveBeenCalledExactlyOnceWith("accepted-turn");
  expect(screen.getByRole("textbox")).toBeEnabled();
});

it("keeps Stop retryable after cancellation fails", async () => {
  const api = fixture(); const user = userEvent.setup();
  api.stopHarnessTurn.mockRejectedValueOnce(new Error("Stop connection lost"));
  api.streamChat.mockImplementation((_body, onEvent, signal) => new Promise((_resolve, reject) => {
    onEvent({ type: "started", harnessTurnId: "harness-only", model: "m" });
    signal.addEventListener("abort", () => reject(new DOMException("Stopped", "AbortError")));
  }));
  render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
  await user.type(screen.getByRole("textbox"), "Question");
  await user.click(screen.getByRole("button", { name: "Ask question" }));
  await user.click(screen.getByRole("button", { name: "Stop response" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Stop connection lost");
  expect(screen.getByRole("textbox")).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Stop response" }));
  expect(screen.getByRole("textbox")).toBeEnabled();
});

it("reports an unconfirmed stop instead of leaving the control disabled forever", async () => {
  const api = fixture(); const user = userEvent.setup();
  api.stopHarnessTurn.mockReturnValue(new Promise(() => {}));
  api.streamChat.mockImplementation((_body, onEvent) => {
    onEvent({ type: "started", harnessTurnId: "harness-only", model: "m" });
    return new Promise(() => {});
  });
  const { unmount } = render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
  await user.type(screen.getByRole("textbox"), "Question");
  await user.click(screen.getByRole("button", { name: "Ask question" }));
  await user.click(screen.getByRole("button", { name: "Stop response" }));
  expect(screen.getByRole("button", { name: "Stop response" })).toBeDisabled();
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Core has not confirmed Stop"), { timeout: 11_000 });
  expect(screen.getByRole("button", { name: "Stop response" })).toBeEnabled();
  unmount();
}, 15_000);


it("renews only while the same popup remains open", async () => {
  const api = fixture();
  const { unmount } = render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
  await waitFor(() => expect(api.createTemporaryChat).toHaveBeenCalledOnce());
  window.dispatchEvent(new Event("focus"));
  await waitFor(() => expect(api.keepTemporaryChatAlive).toHaveBeenCalledExactlyOnceWith("popup"));
  unmount();
  window.dispatchEvent(new Event("focus"));
  expect(api.keepTemporaryChatAlive).toHaveBeenCalledTimes(1);
});


it("retains a question typed before Core finishes opening the branch", async () => {
  const api = fixture(); let resolve!: (session: unknown) => void;
  api.createTemporaryChat.mockReturnValue(new Promise(done => { resolve = done; }));
  const user = userEvent.setup();
  render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {}} />);
  await user.type(screen.getByRole("textbox", { name: "Question for Nebula" }), "A quick question");
  await act(async () => resolve({ id: "popup", backend: "provider", providerId: "provider", model: "m" }));
  expect(screen.getByRole("textbox", { name: "Question for Nebula" })).toHaveValue("A quick question");
  await user.click(screen.getByRole("button", { name: "Ask question" }));
  expect(api.streamChat).toHaveBeenCalledOnce();
  expect(api.streamChat.mock.calls[0][0].messages[0].content).toBe("A quick question");
});

it("hides and restores the same branch, transcript, unsent draft and position", async () => {
  const api = fixture(); const user = userEvent.setup();
  const { unmount } = render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {} } />);
  await user.type(screen.getByRole("textbox", { name: "Question for Nebula" }), "First question");
  await user.click(screen.getByRole("button", { name: "Ask question" }));
  expect(await screen.findByText("A separate answer")).toBeVisible();
  await user.type(screen.getByRole("textbox", { name: "Question for Nebula" }), "An unsent follow-up");
  const popup = screen.getByRole("dialog", { name: "Ask Nebula" });
  const before = popup.style.top;
  await user.click(screen.getByRole("button", { name: "Hide Ask Nebula" }));
  expect(screen.queryByRole("dialog", { name: "Ask Nebula" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Show Ask Nebula, Response ready/ })).toBeVisible();
  expect(screen.getByRole("button", { name: /Show Ask Nebula, Response ready/ })).toHaveFocus();
  expect(api.discardTemporaryChat).not.toHaveBeenCalled();
  window.dispatchEvent(new Event("focus"));
  await waitFor(() => expect(api.keepTemporaryChatAlive).toHaveBeenCalledWith("popup"));
  await user.click(screen.getByRole("button", { name: /Show Ask Nebula/ }));
  expect(screen.getByRole("dialog", { name: "Ask Nebula" }).style.top).toBe(before);
  expect(screen.getByText("A separate answer")).toBeVisible();
  expect(screen.getByRole("textbox", { name: "Question for Nebula" })).toHaveValue("An unsent follow-up");
  expect(api.createTemporaryChat).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: "Ask question" }));
  expect(api.streamChat.mock.calls[1][0].sessionId).toBe("popup");
  unmount();
  expect(api.discardTemporaryChat).toHaveBeenCalledExactlyOnceWith("popup");
});

it("keeps a harness response running while hidden and restores Stop", async () => {
  const api = fixture(); const user = userEvent.setup();
  let sendEvent!: (event: ChatStreamEvent) => void;
  api.streamChat.mockImplementation((_body, onEvent, signal) => new Promise((_resolve, reject) => {
    sendEvent = onEvent;
    onEvent({ type: "started", harnessTurnId: "harness-hidden", model: "m" });
    signal.addEventListener("abort", () => reject(new DOMException("Stopped", "AbortError")));
  }));
  render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {} } />);
  await user.type(screen.getByRole("textbox", { name: "Question for Nebula" }), "Waiting question");
  await user.click(screen.getByRole("button", { name: "Ask question" }));
  await user.click(screen.getByRole("button", { name: "Hide Ask Nebula" }));
  expect(screen.getByRole("button", { name: /Show Ask Nebula, Responding/ })).toBeVisible();
  expect(api.discardTemporaryChat).not.toHaveBeenCalled();
  await act(async () => sendEvent({ type: "output_delta", stream: "commentary", delta: "Still working.", harnessTurnId: "harness-hidden",
    schemaVersion: "nebula.harness-activity/v2", artifactIds: [], payload: {} }));
  await user.click(screen.getByRole("button", { name: /Show Ask Nebula/ }));
  expect(screen.getByRole("status")).toHaveTextContent("Still working.");
  await user.click(screen.getByRole("button", { name: "Stop response" }));
  expect(api.stopHarnessTurn).toHaveBeenCalledExactlyOnceWith("harness-hidden");
  expect(screen.getByRole("textbox", { name: "Question for Nebula" })).toHaveValue("Waiting question");
});

it("shows a completed private answer when a hidden response finishes", async () => {
  const api = fixture(); const user = userEvent.setup();
  let complete!: (response: unknown) => void;
  api.streamChat.mockReturnValue(new Promise(done => { complete = done; }));
  render(<AskNebulaPopup api={api as unknown as ApiClient} snapshot={snapshot} context={context} onClose={() => {} } />);
  await user.type(screen.getByRole("textbox", { name: "Question for Nebula" }), "Finish in the background");
  await user.click(screen.getByRole("button", { name: "Ask question" }));
  await user.click(screen.getByRole("button", { name: "Hide Ask Nebula" }));
  expect(screen.getByRole("button", { name: /Show Ask Nebula, Responding/ })).toBeVisible();
  await act(async () => complete({ message: { role: "assistant", content: "The private answer is ready." } }));
  expect(screen.getByRole("button", { name: /Show Ask Nebula, Response ready/ })).toBeVisible();
  await user.click(screen.getByRole("button", { name: /Show Ask Nebula/ }));
  expect(screen.getByText("The private answer is ready.")).toBeVisible();
  expect(api.createTemporaryChat).toHaveBeenCalledTimes(1);
});
