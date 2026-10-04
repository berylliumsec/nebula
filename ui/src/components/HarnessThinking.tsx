import { useEffect, useRef, useState } from "react";
import { logCaughtDiagnostic } from "../diagnostics";
import { reasoningSummaryState, reasoningSummaryText, type HarnessActivityItem } from "../pages/harnessActivity";
import { HarnessMarkdown } from "./HarnessMarkdown";

export function ThinkingDisclosure({
  text,
  streaming = false,
  load,
}: {
  text?: string;
  streaming?: boolean;
  load?: () => Promise<string>;
}) {
  const thought = text?.trim() ?? "";
  const [expanded, setExpanded] = useState(false);
  const [loaded, setLoaded] = useState<string>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const read = async () => {
    if (!load || loading) return;
    setLoading(true); setError(undefined);
    try { setLoaded(await load()); }
    catch (caught) {
      void logCaughtDiagnostic("interface.assistant_chat.reasoning_failed", "Saved thinking could not be loaded.", caught, "assistant_chat");
      setError(caught instanceof Error ? caught.message : "Could not load saved thinking.");
    }
    finally { setLoading(false); }
  };
  if (!thought && !streaming && !load) return null;
  return <details className="harness-thinking" aria-label="Thinking" onToggle={(event) => {
    setExpanded(event.currentTarget.open);
    if (event.currentTarget.open && load && loaded === undefined && !loading) void read();
  }}>
    <summary>{streaming ? "Thinking…" : "Thinking"}</summary>
    {expanded && (thought || loaded
      ? <div className="harness-reasoning-summary"><HarnessMarkdown content={thought || loaded || ""} /></div>
      : error ? <div role="alert"><span>{error}</span><button className="button quiet" type="button" onClick={() => void read()}>Retry</button></div>
        : <p className="harness-reasoning-note">{loading ? "Loading saved thinking…" : "Waiting for the model…"}</p>)}
  </details>;
}

export function HarnessThinking({ items }: { items: HarnessActivityItem[] }) {
  // Codex can report a reasoning episode without a public summary. Its
  // completed state still belongs in the transcript after the pending flash.
  const thoughts = items.filter((item) => reasoningSummaryState(item) !== undefined);
  const readable = thoughts.filter((item) => reasoningSummaryText(item) || reasoningSummaryState(item) === "pending");
  const summaryCount = thoughts.filter((item) => Boolean(reasoningSummaryText(item))).length;
  const withoutSummary = thoughts.length - readable.length;
  const missingSummarySource = thoughts.filter((item) => reasoningSummaryState(item) === "not_provided")
    .every((item) => item.vendor === "codex_app_server") ? "Codex" : "The harness";
  const [expanded, setExpanded] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const disclosureRef = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (!expanded) return;
    // The thread follows the latest message. Keep the control in view when a
    // long saved summary opens above the reply and changes the scroll height.
    let secondFrame = 0;
    const firstFrame = requestAnimationFrame(() => {
      secondFrame = requestAnimationFrame(() => {
        disclosureRef.current?.querySelector("summary")?.scrollIntoView?.({ block: "start", behavior: "instant" });
      });
    });
    return () => { cancelAnimationFrame(firstFrame); cancelAnimationFrame(secondFrame); };
  }, [expanded]);
  if (!thoughts.length) return null;
  const active = thoughts.some((item) => ["running", "streaming", "pending"].includes(item.status ?? ""));
  const visible = showAll ? readable : readable.slice(-8);
  return <details ref={disclosureRef} className="harness-thinking" aria-label="Harness thinking" onToggle={(event) => {
    setExpanded(event.currentTarget.open);
    if (!event.currentTarget.open) setShowAll(false);
  }}>
    <summary>{active ? "Thinking…" : "Thinking"}<span>{summaryCount > 1 ? `${summaryCount} summaries` : ""}</span></summary>
    {expanded && <div className="harness-thinking-body">
      {withoutSummary > 0 && <p className="harness-reasoning-note">{withoutSummary === 1
        ? `${missingSummarySource} did not provide a public summary for one reasoning episode.`
        : `${missingSummarySource} did not provide public summaries for ${withoutSummary} reasoning episodes.`}</p>}
      {readable.length > 8 && !showAll && <p className="harness-thinking-count">Latest 8 of {readable.length}{withoutSummary ? " summaries" : ""}</p>}
      {visible.length > 0 && <ol className="harness-thinking-list" aria-label="Thinking summaries">
        {visible.map((item) => <li key={item.key}><HarnessMarkdown content={reasoningSummaryText(item) || "Waiting for a summary from the harness…"} /></li>)}
      </ol>}
      {readable.length > 8 && <button className="harness-thinking-more" type="button" onClick={() => setShowAll((value) => !value)}>
        {showAll ? "Show latest 8" : `Show all ${readable.length}`}
      </button>}
    </div>}
  </details>;
}
