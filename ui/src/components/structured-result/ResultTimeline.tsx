import { Bot, CircleUser, Plug, Workflow } from "lucide-react";
import type { StructuredResultSummary } from "../../api/types";
import { StatusChip } from "../SurfacePrimitives";
import { groupStreams } from "./useStructuredResults";

const ORIGIN_ICON = { agent: Bot, tool: Plug, operator: CircleUser, api: Workflow } as const;

export function whenLabel(value: string): string {
  const moment = new Date(value);
  if (Number.isNaN(moment.getTime())) return value;
  const seconds = Math.round((Date.now() - moment.getTime()) / 1_000);
  if (seconds < 45) return "just now";
  if (seconds < 3_600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3_600)} h ago`;
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(moment);
}

export function shapeLabel(item: StructuredResultSummary): string {
  const { rootType, topLevelCount, nodeCount } = item.stats;
  if (rootType === "array") return `${topLevelCount.toLocaleString()} ${topLevelCount === 1 ? "item" : "items"}`;
  if (rootType === "object") return `${topLevelCount.toLocaleString()} ${topLevelCount === 1 ? "field" : "fields"} · ${nodeCount.toLocaleString()} values`;
  return rootType;
}

interface ResultTimelineProps {
  items: StructuredResultSummary[];
  selectedId?: string;
  onSelect: (item: StructuredResultSummary) => void;
  emptyMessage: string;
}

/**
 * Published results in the order they arrived, with the snapshots of one piece
 * of work kept together under the stream key their producer chose.
 */
export function ResultTimeline({ items, selectedId, onSelect, emptyMessage }: ResultTimelineProps) {
  const streams = groupStreams(items);
  if (items.length === 0) return <p className="structured-empty">{emptyMessage}</p>;
  return <div className="structured-timeline">
    {streams.map((stream) => {
      const series = stream.items.length > 1 || stream.items[0].stream !== undefined;
      return <section key={stream.key} className="structured-stream" aria-label={series ? `Stream ${stream.label}` : stream.label}>
        {series && <header>
          <strong>{stream.label}</strong>
          <small>{stream.items.length} {stream.items.length === 1 ? "step" : "steps"} · {whenLabel(stream.items[0].createdAt)}</small>
        </header>}
        <ol>
          {stream.items.map((item) => {
            const Icon = ORIGIN_ICON[item.origin] ?? Workflow;
            return <li key={item.id}>
              <button
                type="button"
                className="structured-timeline-entry"
                aria-current={selectedId === item.id ? "true" : undefined}
                onClick={() => onSelect(item)}
              >
                <span className="structured-timeline-marker" aria-hidden="true"><Icon size={14} /></span>
                <span className="structured-timeline-copy">
                  <strong>{series ? `${item.sequence}. ` : ""}{item.title}</strong>
                  {item.summary && <small>{item.summary}</small>}
                  <small className="structured-timeline-meta">
                    {whenLabel(item.createdAt)} · {shapeLabel(item)}{item.producer ? ` · ${item.producer}` : ""}
                  </small>
                </span>
                {item.hasHints && <StatusChip tone="neutral">hinted</StatusChip>}
              </button>
            </li>;
          })}
        </ol>
      </section>;
    })}
  </div>;
}
