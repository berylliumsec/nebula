import { useCallback, useState } from "react";
import { LoaderCircle } from "lucide-react";
import type { ApiClient } from "../../api/client";
import type { StructuredResultSummary } from "../../api/types";
import { DiagnosticErrorNotice } from "../../diagnostics";
import { StandardEmptyState } from "../SurfacePrimitives";
import { ResultTimeline, shapeLabel, whenLabel } from "./ResultTimeline";
import { StructuredResultDashboard } from "./StructuredResultDashboard";
import { useStructuredResult, useStructuredResults, type StructuredResultDetail, type StructuredResultList } from "./useStructuredResults";

export interface AgentViewStream {
  items: StructuredResultSummary[];
  loading: boolean;
  error?: string;
  refresh: () => void;
  /** The snapshot on screen: the newest unless the operator chose another. */
  current?: string;
  /** True once the operator chose a snapshot, so new ones stop replacing it. */
  pinned: boolean;
  select: (item: StructuredResultSummary) => void;
  followNewest: () => void;
  detail: StructuredResultDetail;
  /** The shown snapshot's place in the conversation, oldest first, from 1. */
  position?: number;
}

/**
 * What this conversation's goal published, following the newest snapshot as
 * it arrives until the operator stops on an earlier one.
 */
export function useAgentViewStream(
  api: ApiClient,
  projectId: string,
  sessionId: string,
  /** The chosen snapshot, when a caller keeps it across remounts. */
  pin?: readonly [string | undefined, (id: string | undefined) => void],
  /**
   * This conversation's results as the page already polls them. Given, the
   * view reads them instead of polling the same list a second time.
   */
  shared?: StructuredResultList,
): AgentViewStream {
  const own = useStructuredResults(shared ? undefined : api, projectId, { chatSessionId: sessionId, live: true, limit: 50 });
  const { items, loading, error, refresh } = shared ?? own;
  const ownPin = useState<string>();
  const [chosen, setChosen] = pin ?? ownPin;
  const pinned = Boolean(chosen && items.some((item) => item.id === chosen));
  const current = pinned ? chosen : items[0]?.id;
  const detail = useStructuredResult(api, projectId, current);
  const index = current ? items.findIndex((item) => item.id === current) : -1;
  const select = useCallback((item: StructuredResultSummary) => {
    // Choosing the newest is following it, not stopping on it.
    setChosen(item.id === items[0]?.id ? undefined : item.id);
  }, [items]);
  const followNewest = useCallback(() => setChosen(undefined), []);
  return {
    items, loading, error, refresh, current, pinned, select, followNewest, detail,
    position: index >= 0 ? items.length - index : undefined,
  };
}

/** One line for the header: whether the view is keeping up, and with what. */
export function agentViewStatus(stream: Pick<AgentViewStream, "items" | "pinned" | "position" | "loading">): string {
  const total = stream.items.length;
  if (total === 0) return stream.loading ? "Reading published snapshots…" : "Waiting for the first snapshot";
  if (stream.pinned && stream.position) return `Stopped on snapshot ${stream.position} of ${total}`;
  return `Following newest · ${total} ${total === 1 ? "snapshot" : "snapshots"}`;
}

/** The timeline and the open snapshot, the same wherever the view is placed. */
export function AgentViewBody({ stream, compact = true }: { stream: AgentViewStream; compact?: boolean }) {
  const { items, loading, error, current, pinned, detail } = stream;
  return <>
    {error && <DiagnosticErrorNotice title="Published results could not be read" error={error} compact />}

    {loading && items.length === 0 && <p role="status"><LoaderCircle className="spin" size={14} aria-hidden="true" /> Reading published results…</p>}

    {!loading && items.length === 0 && <StandardEmptyState
      compact
      title="Nothing published yet"
      explanation="Start a goal and the assistant publishes where its work stands as it goes. Snapshots appear here as soon as they do, with no schema to configure."
    />}

    {items.length > 0 && <div className="chat-result-stream-body">
      <ResultTimeline items={items} selectedId={current} onSelect={stream.select} emptyMessage="Nothing published yet." />
      {pinned && <p className="agent-view-pinned">
        New snapshots will not replace this one.
        <button type="button" className="button quiet" onClick={stream.followNewest}>Back to newest</button>
      </p>}
      {detail.error && <DiagnosticErrorNotice title="That snapshot could not be opened" error={detail.error} compact />}
      {detail.record && <article className="chat-result-stream-detail">
        <header>
          <strong>{detail.record.title}</strong>
          <small>{whenLabel(detail.record.createdAt)} · {shapeLabel(detail.record)}</small>
        </header>
        {detail.record.summary && <p>{detail.record.summary}</p>}
        <StructuredResultDashboard
          key={detail.record.id}
          compact={compact}
          value={detail.record.result}
          hints={detail.record.hints}
          title={detail.record.title}
        />
      </article>}
    </div>}
  </>;
}
