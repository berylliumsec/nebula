import { useState } from "react";
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
    catch (caught) { setError(caught instanceof Error ? caught.message : "Could not load saved thinking."); }
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
  const thoughts = items.filter((item) => reasoningSummaryText(item) || reasoningSummaryState(item) === "pending");
  const [expanded, setExpanded] = useState(false);
  if (!thoughts.length) return null;
  const active = thoughts.some((item) => ["running", "streaming", "pending"].includes(item.status ?? ""));
  return <details className="harness-thinking" aria-label="Harness thinking" onToggle={(event) => setExpanded(event.currentTarget.open)}>
    <summary>{active ? "Thinking…" : "Thinking"}<span>{thoughts.length > 1 ? `${thoughts.length} sections` : ""}</span></summary>
    {expanded && thoughts.map((item) => <HarnessMarkdown content={reasoningSummaryText(item) || "Waiting for a summary from the harness…"} key={item.key} />)}
  </details>;
}
