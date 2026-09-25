import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ApiClient } from "../../api/client";
import { sameJson, startVisiblePoll, type PollOutcome } from "../../api/visiblePoll";
import type { ChatSubagentView } from "../../api/types";
import { logCaughtDiagnostic } from "../../diagnostics";

/** How often an active delegation is read back. */
const POLL_MS = 2_000;
/**
 * While a response runs with no child active yet, the wait doubles up to
 * this: a first child still appears within seconds, at half the requests.
 */
const LIVE_IDLE_POLL_MS = 4_000;
/** Consecutive failures after which polling stops until someone retries. */
const FAILURE_LIMIT = 3;

export const ACTIVE_STATUSES = new Set(["running", "waiting_approval", "recovering"]);

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
 * start the first child at any moment. A hidden page is not polled; it reads
 * again as soon as it is visible. An unchanged list keeps its identity, and a
 * poll sends the validator it last received so Core answers 304 when nothing
 * changed.
 */
export function useChatSubagents(
  api: ApiClient | undefined,
  sessionId: string | undefined,
  { enabled = true, live = false }: { enabled?: boolean; live?: boolean } = {},
): ChatSubagentState {
  const [subagents, setSubagents] = useState<ChatSubagentView[]>([]);
  const shown = useRef<ChatSubagentView[]>(subagents);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const errorShown = useRef(false);
  const [nonce, setNonce] = useState(0);
  const refresh = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    if (!api || !sessionId || !enabled) {
      if (shown.current.length) {
        shown.current = [];
        setSubagents(shown.current);
      }
      errorShown.current = false;
      setError(undefined);
      return;
    }
    const controller = new AbortController();
    let stopped = false;
    let failures = 0;
    let first = true;
    let etag: string | undefined;

    const read = async (signal: AbortSignal): Promise<PollOutcome> => {
      if (first) setLoading(true);
      try {
        const answer = await api.listChatSubagentsIfChanged(sessionId, etag, signal);
        if (stopped) return "stop";
        // 304: Core's list still matches what is shown.
        if (answer) etag = answer.etag;
        const page = answer ? answer.items : shown.current;
        // Setting unchanged state still re-renders the page once; skip it.
        if (errorShown.current) {
          errorShown.current = false;
          setError(undefined);
        }
        failures = 0;
        if (!sameJson(shown.current, page)) {
          shown.current = page;
          setSubagents(page);
        }
        // An idle conversation stops asking; the next response restarts it.
        if (page.some((item) => ACTIVE_STATUSES.has(item.status))) return "active";
        return live ? "idle" : "stop";
      } catch (caught) {
        if (controller.signal.aborted || stopped) return "stop";
        failures += 1;
        void logCaughtDiagnostic("interface.chat_subagents.list_failed", "Delegated subagents could not be read.", caught, "chat_subagents");
        errorShown.current = true;
        setError(caught instanceof Error ? caught.message : "Subagents could not be read.");
        return failures < FAILURE_LIMIT ? "active" : "stop";
      } finally {
        if (!stopped && first) setLoading(false);
        first = false;
      }
    };

    startVisiblePoll({ read, intervalMs: POLL_MS, maxIntervalMs: LIVE_IDLE_POLL_MS, signal: controller.signal });
    return () => {
      stopped = true;
      controller.abort();
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
  const recovery = subagents.filter((item) => item.status === "recovering").length;
  const done = subagents.filter((item) => !ACTIVE_STATUSES.has(item.status)).length;
  return [
    ...(running ? [{ label: `${running} running`, tone: "running" }] : []),
    ...(approval ? [{ label: `${approval} needs approval`, tone: "waiting_approval" }] : []),
    ...(recovery ? [{ label: `${recovery} recovering`, tone: "recovering" }] : []),
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
  recovering: "Recovering",
  completed: "Done",
  failed: "Failed",
  stopped: "Stopped",
  interrupted: "Interrupted",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status.replaceAll("_", " ");
}
