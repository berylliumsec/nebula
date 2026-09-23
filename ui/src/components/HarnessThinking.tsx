import { useState } from "react";
import { reasoningSummaryState, reasoningSummaryText, type HarnessActivityItem } from "../pages/harnessActivity";
import { HarnessMarkdown } from "./HarnessMarkdown";

export function ThinkingDisclosure({
  text,
  streaming = false,
}: {
  text?: string;
  streaming?: boolean;
}) {
  const thought = text?.trim() ?? "";
  const [expanded, setExpanded] = useState(false);
  if (!thought && !streaming) return null;
  return <details className="harness-thinking" aria-label="Thinking" onToggle={(event) => setExpanded(event.currentTarget.open)}>
    <summary>{streaming ? "Thinking…" : "Thinking"}</summary>
    {expanded && (thought
      ? <div className="harness-reasoning-summary"><HarnessMarkdown content={thought} /></div>
      : <p className="harness-reasoning-note">Waiting for the model…</p>)}
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
