import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import { startVisiblePoll, type PollOutcome } from "../api/visiblePoll";
import { logCaughtDiagnostic } from "../diagnostics";

export interface SessionState {
  schema: "nebula.session-state/v1";
  session_id: string;
  revision: number;
  turn_id: string | null;
  harness_turn_id: string | null;
  execution: string;
  busy: boolean;
  detail: string;
  connection: "connected" | "disconnected" | "unknown";
  connection_scope?: "harness_transport";
  actions: ("check_status" | "review" | "stop")[];
  pending: {id: string; turn_id: string; kind: "approval" | "input"; text: string}[];
  decisions: {approval_id: string; status: string; progress?: "not_observed" | "observed"; progress_sequence?: number | null; continuation: {
    harness_turn_id: string; status: "pending" | "delivered" | "failed"; detail?: string;
    adapter_handoff?: "transport_write" | "sdk_callback" | null;
    adapter_status?: "not_required" | "pending" | "sent" | "failed" | "unknown";
    adapter_detail?: string | null;
    progress_after_sequence?: number | null;
  } | null}[];
}

export function acceptSessionState(current: SessionState | undefined, next: SessionState, sessionId: string): SessionState | undefined {
  if (next.session_id !== sessionId) return current;
  if (current?.session_id === sessionId && next.revision < current.revision) return current;
  return current && JSON.stringify(current) === JSON.stringify(next) ? current : next;
}

export function isPendingRequest(snapshot: SessionState | undefined, id: unknown): boolean {
  return typeof id === "string" && (!snapshot || snapshot.pending.some(item => item.id === id));
}

export function pendingApprovalId(snapshot: SessionState | undefined, turnId: string | undefined): string | undefined {
  if (snapshot?.schema !== "nebula.session-state/v1" || !turnId) return undefined;
  return snapshot.pending.find(item => item.kind === "approval" && item.turn_id === turnId)?.id;
}

/** How often the authoritative snapshot is read while a response runs or changes. */
const STATE_POLL_MS = 2_000;
/** An idle, unchanged conversation backs off to this; any change returns to 2 s. */
const STATE_IDLE_POLL_MS = 6_000;

export function useSessionState(api: ApiClient | undefined, sessionId: string | undefined, ready: boolean) {
  const [snapshot, setSnapshot] = useState<SessionState>();
  const [failure, setFailure] = useState<{sessionId: string; text: string}>();
  const refreshRef = useRef<() => void>(() => {});
  const refresh = useCallback(() => refreshRef.current(), []);
  useEffect(() => {
    if (!api || !sessionId || !ready) return;
    const controller = new AbortController();
    const path = `chat/sessions/${encodeURIComponent(sessionId)}/state`;
    // Core answers 304 while this validator still describes its snapshot.
    let etag: string | undefined;
    let last: {revision: number; busy: boolean} | undefined;
    let failed = true; // Clear any failure left by the previous conversation once.
    const poll = startVisiblePoll({
      intervalMs: STATE_POLL_MS,
      maxIntervalMs: STATE_IDLE_POLL_MS,
      signal: controller.signal,
      read: async (signal): Promise<PollOutcome> => {
        try {
          const answer = await api.requestIfChanged<SessionState>(path, etag, {signal});
          if (signal.aborted) return "stop";
          // Setting unchanged state still costs the page a render; skip it.
          if (failed) { failed = false; setFailure(undefined); }
          if (!answer) return last?.busy ? "active" : "idle";
          const next = answer.value;
          if (next?.schema !== "nebula.session-state/v1" || !Number.isSafeInteger(next.revision)
            || !Array.isArray(next.pending) || !Array.isArray(next.decisions)) throw new Error("Unsupported session-state response");
          etag = answer.etag;
          const changed = last?.revision !== next.revision;
          last = {revision: next.revision, busy: next.busy};
          if (changed) setSnapshot(current => acceptSessionState(current, next, sessionId));
          return changed || next.busy ? "active" : "idle";
        } catch (error) {
          if (!signal.aborted) {
            void logCaughtDiagnostic("interface.chat.session_state_unavailable", "Authoritative response status could not be refreshed.", error, "sessions_page");
            failed = true;
            setFailure({sessionId, text: "Response status could not sync. Check the connection and retry."});
          }
          return "active";
        }
      },
    });
    refreshRef.current = poll.poke;
    window.addEventListener("online", poll.poke);
    return () => {
      controller.abort(); refreshRef.current = () => {};
      window.removeEventListener("online", poll.poke);
    };
  }, [api, sessionId, ready]);
  return {state: snapshot?.session_id === sessionId && ready ? snapshot : undefined,
    error: failure && failure.sessionId === sessionId && ready ? failure.text : undefined, refresh};
}
