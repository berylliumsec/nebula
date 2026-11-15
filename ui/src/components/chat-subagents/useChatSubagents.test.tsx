import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { ChatSubagentView } from "../../api/types";
import { useChatSubagents } from "./useChatSubagents";
import { PROJECT_RUNTIME_UNAVAILABLE, ProjectRuntimeRecovery } from "../../api/projectRuntime";

vi.mock("../../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

let visibility: DocumentVisibilityState = "visible";

function child(status: string): ChatSubagentView {
  return { id: "child", status, title: "Research the host" } as unknown as ChatSubagentView;
}

/** Core's conditional list: every answer is new content with a fresh validator. */
function listing(status: string) {
  let version = 0;
  return vi.fn(async (_session: string, _etag: string | undefined) => ({ items: [child(status)], etag: `"subagents-${++version}"` }));
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
    const listChatSubagents = listing("running");
    const api = { listChatSubagentsIfChanged: listChatSubagents } as unknown as ApiClient;
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
    const listChatSubagents = listing("completed");
    const api = { listChatSubagentsIfChanged: listChatSubagents } as unknown as ApiClient;
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

  it("sends the validator it last received and keeps the shown list when Core answers 304", async () => {
    const running = [child("running")];
    const listChatSubagentsIfChanged = vi.fn(async (_session: string, etag: string | undefined) => (
      etag === '"subagents-a"' ? undefined : { items: running, etag: '"subagents-a"' }
    ));
    const api = { listChatSubagentsIfChanged } as unknown as ApiClient;
    let renders = 0;
    const { result } = renderHook(() => { renders += 1; return useChatSubagents(api, "parent"); });
    await advance(0);
    const first = result.current.subagents;
    expect(first).toEqual(running);
    const settled = renders;
    await advance(6_000);
    // Each later poll is conditional; a 304 keeps a running child polling at 2 s.
    expect(listChatSubagentsIfChanged.mock.calls.map(([, etag]) => etag)).toEqual([undefined, '"subagents-a"', '"subagents-a"', '"subagents-a"']);
    expect(result.current.subagents).toBe(first);
    expect(result.current.active).toHaveLength(1);
    expect(renders).toBe(settled);
  });

  it("reads the whole list again after a refresh", async () => {
    const listChatSubagentsIfChanged = vi.fn(async (_session: string, etag: string | undefined) => (
      etag ? undefined : { items: [child("completed")], etag: '"subagents-a"' }
    ));
    const api = { listChatSubagentsIfChanged } as unknown as ApiClient;
    const { result } = renderHook(() => useChatSubagents(api, "parent", { live: true }));
    await advance(0);
    // Nothing active while a response runs: the next read waits 4 s.
    await advance(4_000);
    act(() => result.current.refresh());
    await advance(0);
    expect(listChatSubagentsIfChanged.mock.calls.map(([, etag]) => etag)).toEqual([undefined, '"subagents-a"', undefined]);
    expect(result.current.subagents).toEqual([child("completed")]);
  });

  it("stops asking once nothing is active and no response runs", async () => {
    const listChatSubagents = listing("completed");
    const api = { listChatSubagentsIfChanged: listChatSubagents } as unknown as ApiClient;
    renderHook(() => useChatSubagents(api, "parent"));
    await advance(0);
    await advance(30_000);
    expect(listChatSubagents).toHaveBeenCalledTimes(1);
  });

  it("pauses without an error while the project's worker is away and resumes when it answers", async () => {
    let away = true;
    const recovery = new ProjectRuntimeRecovery(async () => ({ answered: !away }));
    const list = vi.fn(async (session: string) => {
      const path = `chat/sessions/${session}/subagents`;
      if (away) {
        recovery.unavailable(path);
        throw Object.assign(new Error("idle"), { status: 503, code: PROJECT_RUNTIME_UNAVAILABLE });
      }
      recovery.answered(path);
      return { items: [child("running")], etag: "\"one\"" };
    });
    const api = { listChatSubagentsIfChanged: list, projectRuntime: recovery } as unknown as ApiClient;
    const { result } = renderHook(() => useChatSubagents(api, "parent"));
    await advance(0);
    await advance(10_000);
    // One read, then the shared probe waits instead of three failures and a stopped poll.
    expect(list).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeUndefined();
    expect(recovery.state).toBe("reconnecting");
    away = false;
    await advance(6_000);
    expect(list).toHaveBeenCalledTimes(2);
    expect(result.current.active).toHaveLength(1);
    expect(result.current.error).toBeUndefined();
  });
});
