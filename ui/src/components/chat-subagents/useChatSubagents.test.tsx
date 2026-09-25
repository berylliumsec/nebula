import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { ChatSubagentView } from "../../api/types";
import { useChatSubagents } from "./useChatSubagents";

vi.mock("../../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

let visibility: DocumentVisibilityState = "visible";

function child(status: string): ChatSubagentView {
  return { id: "child", status, title: "Research the host" } as unknown as ChatSubagentView;
}

async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

describe("subagent rail polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  });
  afterEach(() => vi.useRealTimers());

  it("keeps a working child at the 2 s cadence and pauses while the page is hidden", async () => {
    const listChatSubagents = vi.fn(async () => [child("running")]);
    const api = { listChatSubagents } as unknown as ApiClient;
    renderHook(() => useChatSubagents(api, "parent"));
    await advance(0);
    await advance(6_000);
    expect(listChatSubagents).toHaveBeenCalledTimes(4);

    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    await advance(60_000);
    expect(listChatSubagents).toHaveBeenCalledTimes(4);

    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await advance(0);
    expect(listChatSubagents).toHaveBeenCalledTimes(5);
  });

  it("backs off while a response runs with no child active, and keeps an unchanged list's identity", async () => {
    const listChatSubagents = vi.fn(async () => [child("completed")]);
    const api = { listChatSubagents } as unknown as ApiClient;
    let renders = 0;
    const { result } = renderHook(() => { renders += 1; return useChatSubagents(api, "parent", { live: true }); });
    await advance(0);
    const first = result.current.subagents;
    const settled = renders;
    await advance(20_000);
    // Waits of 2 s, then 4 s: six reads in 20 s rather than eleven.
    expect(listChatSubagents).toHaveBeenCalledTimes(6);
    expect(result.current.subagents).toBe(first);
    expect(renders).toBe(settled);
  });

  it("stops asking once nothing is active and no response runs", async () => {
    const listChatSubagents = vi.fn(async () => [child("completed")]);
    const api = { listChatSubagents } as unknown as ApiClient;
    renderHook(() => useChatSubagents(api, "parent"));
    await advance(0);
    await advance(30_000);
    expect(listChatSubagents).toHaveBeenCalledTimes(1);
  });
});
