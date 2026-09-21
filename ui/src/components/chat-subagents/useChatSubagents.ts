import { useCallback, useEffect, useMemo, useState } from "react";
import type { ApiClient } from "../../api/client";
import type { ChatSubagentView } from "../../api/types";
import { logCaughtDiagnostic } from "../../diagnostics";

/** How often an active delegation is read back. */
const POLL_MS = 2_000;
/** Consecutive failures after which polling stops until someone retries. */
const FAILURE_LIMIT = 3;

/** Core's running limit per conversation, mirrored for the slot readout. */
export const SUBAGENT_SLOTS = 3;

export const ACTIVE_STATUSES = new Set(["running", "waiting_approval"]);

export interface ChatSubagentState {
  subagents: ChatSubagentView[];
  active: ChatSubagentView[];
  loading: boolean;
  error?: string;
  refresh: () => void;
}

/**
 * The delegated children of one conversation.
 *
 * Core owns their lifecycle; this only reads. Polling runs while any child is
 * active and stops when they all finish, so an idle conversation costs nothing.
 * `live` keeps it polling while a response runs, because that response may
 * start the first child at any moment.
 */
export function useChatSubagents(
  api: ApiClient | undefined,
  sessionId: string | undefined,
  { enabled = true, live = false }: { enabled?: boolean; live?: boolean } = {},
): ChatSubagentState {
  const [subagents, setSubagents] = useState<ChatSubagentView[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [nonce, setNonce] = useState(0);
  const refresh = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    if (!api || !sessionId || !enabled) {
      setSubagents([]);
      setError(undefined);
      return;
    }
    const controller = new AbortController();
    let timer: number | undefined;
    let stopped = false;
    let failures = 0;

    const read = async (first: boolean) => {
      if (first) setLoading(true);
      let keepPolling = false;
      try {
        const page = await api.listChatSubagents(sessionId, controller.signal);
        if (stopped) return;
        failures = 0;
        setSubagents(page);
        setError(undefined);
        keepPolling = live || page.some((item) => ACTIVE_STATUSES.has(item.status));
      } catch (caught) {
        if (controller.signal.aborted || stopped) return;
        failures += 1;
        void logCaughtDiagnostic("interface.chat_subagents.list_failed", "Delegated subagents could not be read.", caught, "chat_subagents");
        setError(caught instanceof Error ? caught.message : "Subagents could not be read.");
        keepPolling = failures < FAILURE_LIMIT;
      } finally {
        if (!stopped) setLoading(false);
        // An idle conversation stops asking; the next response restarts it.
        if (!stopped && keepPolling) timer = window.setTimeout(() => void read(false), POLL_MS);
      }
    };

    void read(true);
    return () => {
      stopped = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [api, enabled, live, nonce, sessionId]);

  const active = useMemo(
    () => subagents.filter((item) => ACTIVE_STATUSES.has(item.status)),
    [subagents],
  );
  return { subagents, active, loading, error, refresh };
}

/** "1 running · 1 needs approval · 1 done", omitting what is not there. */
export function subagentSummary(subagents: ChatSubagentView[]): { label: string; tone: string }[] {
  const running = subagents.filter((item) => item.status === "running").length;
  const approval = subagents.filter((item) => item.status === "waiting_approval").length;
  const done = subagents.filter((item) => !ACTIVE_STATUSES.has(item.status)).length;
  return [
    ...(running ? [{ label: `${running} running`, tone: "running" }] : []),
    ...(approval ? [{ label: `${approval} needs approval`, tone: "waiting_approval" }] : []),
    ...(done ? [{ label: `${done} done`, tone: "done" }] : []),
  ];
}

/** Compact token counts, the way the rest of the assistant shows them. */
export function compactTokens(total: number): string {
  if (total < 1_000) return String(total);
  return `${(total / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
}

export function elapsedLabel(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  return `${minutes}m ${String(whole % 60).padStart(2, "0")}s`;
}

const STATUS_LABELS: Record<string, string> = {
  running: "Running",
  waiting_approval: "Needs approval",
  completed: "Done",
  failed: "Failed",
  stopped: "Stopped",
  interrupted: "Interrupted",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status.replaceAll("_", " ");
}
