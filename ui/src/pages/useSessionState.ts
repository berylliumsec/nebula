import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
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
  decisions: {approval_id: string; status: string; continuation: {
    harness_turn_id: string; status: "pending" | "delivered" | "failed"; detail?: string;
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

export function useSessionState(api: ApiClient | undefined, sessionId: string | undefined, ready: boolean) {
  const [snapshot, setSnapshot] = useState<SessionState>();
  const [failure, setFailure] = useState<{sessionId: string; text: string}>();
  const refreshRef = useRef<() => void>(() => {});
  const refresh = useCallback(() => refreshRef.current(), []);
  useEffect(() => {
    if (!api || !sessionId || !ready) return;
    const controller = new AbortController();
    let busy = false; let queued = false;
    const read = async () => {
      if (controller.signal.aborted) return;
      if (busy) { queued = true; return; }
      busy = true;
      try {
        const next = await api.request<SessionState>(`chat/sessions/${encodeURIComponent(sessionId)}/state`, {signal: controller.signal});
        if (controller.signal.aborted) return;
        if (next?.schema !== "nebula.session-state/v1" || !Number.isSafeInteger(next.revision)
          || !Array.isArray(next.pending) || !Array.isArray(next.decisions)) throw new Error("Unsupported session-state response");
        setSnapshot(current => acceptSessionState(current, next, sessionId));
        setFailure(undefined);
      } catch (error) {
        if (!controller.signal.aborted) {
          void logCaughtDiagnostic("interface.chat.session_state_unavailable", "Authoritative response status could not be refreshed.", error, "sessions_page");
          setFailure({sessionId, text: "Response status could not sync. Check the connection and retry."});
        }
      } finally {
        busy = false;
        if (queued && !controller.signal.aborted) {queued = false; void read();}
      }
    };
    const trigger = () => {void read();};
    const visible = () => {if (!document.hidden) trigger();};
    refreshRef.current = trigger;
    trigger();
    const timer = setInterval(visible, 2000);
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("online", trigger);
    return () => {
      controller.abort(); clearInterval(timer); refreshRef.current = () => {};
      document.removeEventListener("visibilitychange", visible); window.removeEventListener("online", trigger);
    };
  }, [api, sessionId, ready]);
  return {state: snapshot?.session_id === sessionId && ready ? snapshot : undefined,
    error: failure && failure.sessionId === sessionId && ready ? failure.text : undefined, refresh};
}
