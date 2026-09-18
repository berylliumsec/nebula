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
  if (!thought && !streaming) return null;
  return <details className="harness-thinking" aria-label="Thinking">
    <summary>{streaming ? "Thinking…" : "Thinking"}</summary>
    {thought
      ? <div className="harness-reasoning-summary"><HarnessMarkdown content={thought} /></div>
      : <p className="harness-reasoning-note">Waiting for the model…</p>}
  </details>;
}

export function HarnessThinking({ items }: { items: HarnessActivityItem[] }) {
  const thoughts = items.filter((item) => reasoningSummaryText(item) || reasoningSummaryState(item) === "pending");
  if (!thoughts.length) return null;
  const active = thoughts.some((item) => ["running", "streaming", "pending"].includes(item.status ?? ""));
  return <details className="harness-thinking" aria-label="Harness thinking">
    <summary>{active ? "Thinking…" : "Thinking"}<span>{thoughts.length > 1 ? `${thoughts.length} sections` : ""}</span></summary>
    {thoughts.map((item) => <HarnessMarkdown content={reasoningSummaryText(item) || "Waiting for a summary from the harness…"} key={item.key} />)}
  </details>;
}
