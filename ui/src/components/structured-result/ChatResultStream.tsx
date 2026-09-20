import { useState } from "react";
import { ExternalLink, LoaderCircle } from "lucide-react";
import { Link } from "react-router-dom";
import type { ApiClient } from "../../api/client";
import type { StructuredResultSummary } from "../../api/types";
import { DiagnosticErrorNotice } from "../../diagnostics";
import { projectSurface } from "../../resourceRoutes";
import { StandardEmptyState } from "../SurfacePrimitives";
import { ResultTimeline, shapeLabel, whenLabel } from "./ResultTimeline";
import { StructuredResultDashboard } from "./StructuredResultDashboard";
import { useStructuredResult, useStructuredResults } from "./useStructuredResults";

interface ChatResultStreamProps {
  api: ApiClient;
  projectId: string;
  sessionId: string;
}

/**
 * What the agent published while working on this conversation, beside the
 * conversation. It is the same explorer as the Results surface, narrowed to
 * this session and following the newest snapshot as it arrives.
 */
export function ChatResultStream({ api, projectId, sessionId }: ChatResultStreamProps) {
  const { items, loading, error, refresh } = useStructuredResults(api, projectId, { chatSessionId: sessionId, live: true, limit: 50 });
  const [pinned, setPinned] = useState<string>();
  const current = pinned && items.some((item) => item.id === pinned) ? pinned : items[0]?.id;
  const detail = useStructuredResult(api, projectId, current);

  const select = (item: StructuredResultSummary) => setPinned(item.id);

  return <section className="chat-result-stream" aria-label="Published results for this conversation">
    <header>
      <div>
        <h3>Agent view</h3>
        <p>What this conversation's goal published as it worked. New snapshots appear as they arrive.</p>
      </div>
      <Link className="button quiet" to={projectSurface(projectId, "results", current)}>
        <ExternalLink size={14} aria-hidden="true" /> Open in Results
      </Link>
    </header>

    {error && <DiagnosticErrorNotice title="Published results could not be read" error={error} compact />}

    {loading && items.length === 0 && <p role="status"><LoaderCircle className="spin" size={14} aria-hidden="true" /> Reading published results…</p>}

    {!loading && items.length === 0 && <StandardEmptyState
      compact
      title="Nothing published yet"
      explanation="Start a goal and the assistant publishes where its work stands as it goes. Snapshots appear here as soon as they do, with no schema to configure."
    />}

    {items.length > 0 && <div className="chat-result-stream-body">
      <ResultTimeline items={items} selectedId={current} onSelect={select} emptyMessage="Nothing published yet." />
      {detail.error && <DiagnosticErrorNotice title="That snapshot could not be opened" error={detail.error} compact />}
      {detail.record && <article className="chat-result-stream-detail">
        <header>
          <strong>{detail.record.title}</strong>
          <small>{whenLabel(detail.record.createdAt)} · {shapeLabel(detail.record)}</small>
        </header>
        {detail.record.summary && <p>{detail.record.summary}</p>}
        <StructuredResultDashboard
          key={detail.record.id}
          compact
          value={detail.record.result}
          hints={detail.record.hints}
          title={detail.record.title}
        />
      </article>}
      {!pinned && items.length > 1 && <p className="structured-derived">Showing the newest snapshot. Choose an earlier step above to stay on it.</p>}
      <button type="button" className="button quiet" onClick={refresh}>Refresh now</button>
    </div>}
  </section>;
}
