import { LoaderCircle } from "lucide-react";
import type { ContextMemory, ContextMemoryItem, ContextStatus, ContextWorkingNotes, ExecutionLanguage } from "../api/types";
import { AssistantMarkdown } from "./AssistantMarkdown";
import { ProviderRequestInputDetails } from "./ProviderRequestInputDetails";
import { whenLabel } from "./structured-result/ResultTimeline";

const NO_RUNNABLE_LANGUAGES = new Set<ExecutionLanguage>();
const ignoreRun = () => undefined;

/** Share of the target input the next request is estimated to use, when Core owns the context. */
export function contextUsePercent(status: ContextStatus | undefined): number | undefined {
  return status && status.status !== "runtime_managed" && status.targetInputTokens > 0
    ? Math.min(100, Math.round((status.estimatedInputTokens / status.targetInputTokens) * 100))
    : undefined;
}

/** Where the window size comes from, so an estimated limit never reads as exact. */
export function contextCapacityLabel(status: ContextStatus): string {
  const window = status.contextWindow.toLocaleString();
  // Core names the limit that sets the window; a window below the model's or
  // the routes' own leads, and the facts it narrows follow.
  const configured = status.bindingLimit === "configured" ? `${window} configured cap` : undefined;
  if (status.routeLimitsRequired && status.routeLimitsVerified) {
    const lead = configured ?? (status.bindingLimit === "model" ? `${window} model window` : undefined);
    const input = status.inputCapacity === undefined ? [] : [`${status.inputCapacity.toLocaleString()} input ceiling`];
    const routes = [`${status.eligibleRouteCount ?? 0} compatible routes`, `${(status.routeContextWindow ?? status.contextWindow).toLocaleString()} route minimum`];
    return (lead ? [lead, ...input, ...routes] : [...routes, ...input]).join(" · ");
  }
  if (status.routeLimitsRequired) return configured ? `${configured} · route limits unverified` : `route limits unverified · safe ${window}-token ceiling`;
  const source = status.capacitySource === "model_catalog" ? "exact model catalog"
    : status.capacitySource === "known_model" ? "published model limits"
      : status.capacitySource === "configured" ? "configured estimate"
        : "safe fallback estimate";
  return configured && status.capacitySource !== "configured" ? `${configured} · ${source}` : source;
}

type MemorySection = { label: string; text?: string; items?: ContextMemoryItem[] };

/** The saved memory's non-empty sections, in the order the compactor writes them. */
export function memorySections(memory: ContextMemory): MemorySection[] {
  const sections: MemorySection[] = [
    { label: "Objective", text: memory.objective },
    { label: "Summary", text: memory.summary },
    { label: "Operator requests", items: memory.userRequests },
    { label: "Current state and next steps", items: memory.currentState },
    { label: "Decisions", items: memory.decisions },
    { label: "Constraints", items: memory.constraints },
    { label: "Confirmed facts", items: memory.confirmedFacts },
    { label: "Attempts", items: memory.attempts },
    { label: "Corrections", items: memory.corrections },
    { label: "References", items: memory.references },
    { label: "Open questions", items: memory.openQuestions },
  ];
  return sections.filter((section) => section.items ? section.items.length > 0 : Boolean(section.text?.trim()));
}

const plural = (count: number, noun: string) => `${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}`;

/** A reduced-quality qualifier for the status line, or nothing for a complete memory. */
function qualityQualifier(status: ContextStatus): string | undefined {
  if (status.status !== "ready" && status.status !== "stale") return undefined;
  if (status.quality === "degraded") return "partial summary";
  const dropped = status.snapshot?.droppedItems ?? 0;
  if (status.quality === "salvaged" && dropped > 0) return `${plural(dropped, "item")} removed`;
  return undefined;
}

function AgentNotes({ notes }: { notes: ContextWorkingNotes }) {
  const updated = notes.updatedAt && !Number.isNaN(Date.parse(notes.updatedAt)) ? notes.updatedAt : undefined;
  return <details className="session-memory session-agent-notes">
    <summary>Agent notes{updated && <span className="session-disclosure-meta"> · updated <time dateTime={updated} title={new Date(updated).toLocaleString()}>{whenLabel(updated)}</time></span>}</summary>
    <div>
      <div className="session-agent-notes-body">
        <AssistantMarkdown content={notes.content} durable={false} runnableLanguages={NO_RUNNABLE_LANGUAGES} onRun={ignoreRun} />
      </div>
      <small>Kept by the assistant for this conversation{notes.revision > 0 ? ` · revision ${notes.revision}` : ""} · read-only here</small>
    </div>
  </details>;
}

/**
 * The Working context readout in Conversation details: how full the next
 * request is, what older messages were compacted into, and the notes the
 * assistant keeps. Core's context status is the only authority; this renders it.
 */
export function WorkingContextPanel({ hasSession, loading, error, status, onRetry }: {
  hasSession: boolean;
  loading: boolean;
  error?: string;
  status?: ContextStatus;
  onRetry: () => void;
}) {
  const percent = contextUsePercent(status);
  const managed = status?.status === "runtime_managed";
  const qualifier = status ? qualityQualifier(status) : undefined;
  const degraded = qualifier !== undefined && status?.quality === "degraded";
  const memory = status?.snapshot?.memory;
  const sections = memory ? memorySections(memory) : [];
  const sourceCount = status?.snapshot?.sourceReferences.length ?? 0;
  const tone = status?.status === "failed" ? "unavailable"
    : qualifier ? "partial"
      : status?.status === "stale" ? "pending" : "healthy";
  const statusLine = !status ? "" : managed
    ? "The selected harness owns compaction and reports its usage through activity."
    : degraded
      ? "The automatic summary failed, so Nebula kept every operator request and exact identifier from the older messages. The assistant can still search the originals. The summary is rebuilt at the next compaction."
      : `${status.estimatedInputTokens.toLocaleString()} of ${status.targetInputTokens.toLocaleString()} target input tokens${status.estimateCalibration !== undefined ? " · calibrated from provider usage" : ""} · ${contextCapacityLabel(status)}`;

  return <>
    <section className="session-context-health"><h3>Working context</h3>{!hasSession ? <p>Context becomes durable after the first saved turn.</p>
      : loading && !status ? <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Reading Core context…</div>
        : error ? <div className="session-context-error"><p>{error}</p><button className="button quiet" type="button" onClick={onRetry}>Retry</button></div>
          : status ? <>
            <div className="session-context-summary">
              <span className={`status-dot ${tone}`} aria-hidden="true" />
              <div><strong>{managed ? "Harness managed" : status.status.replaceAll("_", " ")}{qualifier && <span className="session-context-quality"> · {qualifier}</span>}</strong><small>{statusLine}</small></div>
            </div>
            {percent !== undefined && <div className="session-context-progress" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-label={`${percent} percent of target input used`}><span style={{ width: `${percent}%` }} /></div>}
            {status.compactedThrough > 0 && <p>Compacted through message {status.compactedThrough}. The original messages are unchanged and searchable.</p>}
            {memory && <details className="session-memory">
              <summary>Inspect saved memory<span className="session-disclosure-meta"> · {degraded ? "requests and identifiers only" : `${plural(sections.length, "section")}, ${plural(sourceCount, "source")}`}</span></summary>
              <div>
                {sections.map((section) => <section key={section.label}><strong>{section.label}</strong>{section.items
                  ? <ul>{section.items.map((item, index) => <li key={`${section.label}-${index}`}>{item.text}</li>)}</ul>
                  : <p>{section.text}</p>}</section>)}
                <small>{sourceCount} source reference{sourceCount === 1 ? "" : "s"} · private reasoning is not stored</small>
              </div>
            </details>}
            {status.workingNotes && <AgentNotes notes={status.workingNotes} />}
          </> : <p>Context status has not been recorded yet.</p>}</section>
    {status && !managed && <section className="session-context-health">
      <p>The meter estimates the next request: conversation, instructions and tool definitions{status.estimateCalibration !== undefined ? ", corrected by the input tokens the provider reported last time" : ""}.</p>
      {status.lastProviderRequest && <ProviderRequestInputDetails request={status.lastProviderRequest} />}
    </section>}
  </>;
}
