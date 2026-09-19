import { ChevronRight, Sparkles } from "lucide-react";
import type { ToolSuggestionSummary } from "../api/types";

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function unavailableReason(error: string | undefined): string {
  if (!error) return "Jev unavailable";
  if (/timeout|timed out/i.test(error)) return "Jev timed out";
  if (/no typesafe key/i.test(error)) return "no TypeSafe key";
  return "Jev unavailable";
}

export function toolSuggestionLabel(summary: ToolSuggestionSummary): string {
  if (summary.status === "unavailable") return `No suggestions · ${unavailableReason(summary.error)}`;
  const total = summary.preloaded.length + summary.suggested.length;
  if (!total) return "No tools suggested";
  return summary.preloaded.length
    ? `${plural(total, "tool")} suggested · ${summary.preloaded.length} preloaded`
    : `${plural(total, "tool")} suggested`;
}

interface Row { name: string; status: string; tone: "good" | "info" | "muted" }

export function toolSuggestionRows(summary: ToolSuggestionSummary): Row[] {
  const used = new Set(summary.used);
  return [
    ...summary.preloaded.map((name): Row => ({ name, status: "Preloaded", tone: "good" })),
    ...summary.suggested.map((name): Row => used.has(name)
      ? { name, status: "Suggested · used", tone: "info" }
      : { name, status: "Suggested · not used", tone: "muted" }),
    ...summary.loadedByModel.map((name): Row => ({ name, status: "Loaded by the model", tone: "info" })),
  ];
}

/** Collapsed per-turn summary of Jev's suggestions; it never asks for action. */
export function ToolSuggestionChip({ summary }: { summary: ToolSuggestionSummary }) {
  const rows = toolSuggestionRows(summary);
  const unavailable = summary.status === "unavailable";
  const meta = [
    summary.model ? summary.model.replace(/^jev-/, "Jev ") : undefined,
    summary.latencyMs !== undefined ? `${summary.latencyMs} ms` : undefined,
  ].filter(Boolean).join(" · ");
  return <details className={`tool-suggestion-chip${unavailable ? " unavailable" : ""}`}>
    <summary>
      <Sparkles size={12} aria-hidden="true" />
      <span>{toolSuggestionLabel(summary)}</span>
      <ChevronRight className="tool-suggestion-chevron" size={13} aria-hidden="true" />
    </summary>
    <div className="tool-suggestion-body">
      <strong>Tool suggestions for this turn</strong>
      {rows.length > 0 && <ul>
        {rows.map((row) => <li key={`${row.status}:${row.name}`} className={row.tone}>
          <span aria-hidden="true" />
          <code>{row.name}</code>
          <small>{row.status}</small>
        </li>)}
      </ul>}
      {unavailable
        ? <p>The turn still ran. {plural(summary.onDemandCount, "on-demand tool")} stayed available through the catalog.{summary.error ? ` Reason: ${summary.error}` : ""}</p>
        : <p>{summary.unloadedCount > 0 ? `${summary.unloadedCount} more on-demand ${summary.unloadedCount === 1 ? "tool" : "tools"} stayed unloaded` : "No other on-demand tools"}{meta ? ` · ${meta}` : ""}</p>}
    </div>
  </details>;
}
