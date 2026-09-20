import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ApiClient } from "../../api/client";
import type { StructuredResultRecord, StructuredResultSummary } from "../../api/types";
import { logCaughtDiagnostic } from "../../diagnostics";

/** How often an open, following surface asks Core for newly published results. */
const POLL_MS = 4_000;
/** Consecutive failures after which polling stops until someone retries. */
const FAILURE_LIMIT = 3;

interface ListOptions {
  chatSessionId?: string;
  stream?: string;
  /** Keep asking for new results while this surface is open. */
  live?: boolean;
  limit?: number;
}

export interface StructuredResultList {
  items: StructuredResultSummary[];
  loading: boolean;
  error?: string;
  refresh: () => void;
}

/**
 * The published results of a project, optionally narrowed to one conversation
 * or one stream. Core is the authority; this only reads, and a failed refresh
 * keeps the last good list on screen rather than blanking it.
 */
export function useStructuredResults(
  api: ApiClient | undefined,
  projectId: string | undefined,
  options: ListOptions = {},
): StructuredResultList {
  const { chatSessionId, stream, live = false, limit = 100 } = options;
  const [items, setItems] = useState<StructuredResultSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [nonce, setNonce] = useState(0);
  const refresh = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    if (!api || !projectId) {
      setItems([]);
      return;
    }
    const controller = new AbortController();
    let timer: number | undefined;
    let stopped = false;
    let failures = 0;

    const read = async (first: boolean) => {
      if (first) setLoading(true);
      try {
        const page = await api.listStructuredResults(projectId, { chatSessionId, stream, limit }, controller.signal);
        if (stopped) return;
        failures = 0;
        setItems(page);
        setError(undefined);
      } catch (caught) {
        if (controller.signal.aborted || stopped) return;
        failures += 1;
        void logCaughtDiagnostic("interface.structured_result.list_failed", "Published results could not be read.", caught, "structured_result");
        setError(caught instanceof Error ? caught.message : "Published results could not be read.");
      } finally {
        if (!stopped) setLoading(false);
        // A surface that keeps failing stops asking; Refresh starts it again.
        if (!stopped && live && failures < FAILURE_LIMIT) timer = window.setTimeout(() => void read(false), POLL_MS);
      }
    };

    void read(true);
    return () => {
      stopped = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [api, chatSessionId, limit, live, nonce, projectId, stream]);

  return { items, loading, error, refresh };
}

export interface StructuredResultDetail {
  record?: StructuredResultRecord;
  loading: boolean;
  error?: string;
}

/** One published result, fetched only when an operator opens it. */
export function useStructuredResult(
  api: ApiClient | undefined,
  projectId: string | undefined,
  resultId: string | undefined,
): StructuredResultDetail {
  const [record, setRecord] = useState<StructuredResultRecord>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (!api || !projectId || !resultId) {
      setRecord(undefined);
      setError(undefined);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    api.getStructuredResult(projectId, resultId, controller.signal)
      .then((value) => {
        setRecord(value);
        setError(undefined);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        void logCaughtDiagnostic("interface.structured_result.read_failed", "A published result could not be read.", caught, "structured_result");
        setRecord(undefined);
        setError(caught instanceof Error ? caught.message : "That result could not be read.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [api, projectId, resultId]);

  return { record, loading, error };
}

/** Results grouped into the streams producers published them under. */
export interface ResultStream {
  key: string;
  label: string;
  items: StructuredResultSummary[];
  latest: StructuredResultSummary;
}

export function groupStreams(items: StructuredResultSummary[]): ResultStream[] {
  const streams = new Map<string, StructuredResultSummary[]>();
  for (const item of items) {
    const key = item.stream ?? `result:${item.id}`;
    streams.set(key, [...(streams.get(key) ?? []), item]);
  }
  return [...streams.entries()].map(([key, grouped]) => ({
    key,
    // A goal's series is keyed by its identity; the objective is what reads.
    label: grouped[0].streamLabel ?? grouped[0].stream ?? grouped[0].title,
    items: [...grouped].sort((left, right) => right.sequence - left.sequence),
    latest: grouped[0],
  }));
}

/**
 * How many results arrived since the operator last looked, so a toggle can say
 * so without pulling attention mid-turn.
 */
export function useUnseenCount(items: StructuredResultSummary[], open: boolean): { unseen: number; acknowledge: () => void } {
  const seen = useRef<Set<string>>(new Set());
  const [unseen, setUnseen] = useState(0);
  const acknowledge = useCallback(() => {
    for (const item of items) seen.current.add(item.id);
    setUnseen(0);
  }, [items]);

  useEffect(() => {
    if (open) {
      for (const item of items) seen.current.add(item.id);
      setUnseen(0);
      return;
    }
    setUnseen(items.filter((item) => !seen.current.has(item.id)).length);
  }, [items, open]);

  return useMemo(() => ({ unseen, acknowledge }), [acknowledge, unseen]);
}
