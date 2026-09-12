import { reasoningSummaryState, reasoningSummaryText, type HarnessActivityItem } from "../pages/harnessActivity";

export function HarnessThinking({ items }: { items: HarnessActivityItem[] }) {
  const thoughts = items.filter((item) => reasoningSummaryText(item) || reasoningSummaryState(item) === "pending");
  if (!thoughts.length) return null;
  const active = thoughts.some((item) => ["running", "streaming", "pending"].includes(item.status ?? ""));
  return <details className="harness-thinking" aria-label="Harness thinking">
    <summary>{active ? "Thinking…" : "Thinking"}<span>{thoughts.length > 1 ? `${thoughts.length} sections` : ""}</span></summary>
    {thoughts.map((item) => <p className="harness-reasoning-summary" key={item.key}>{reasoningSummaryText(item) || "Waiting for a summary from the harness…"}</p>)}
  </details>;
}
