import { Bot, DollarSign, FileCheck2, Target } from "lucide-react";
import type { AgentRunSummary, AssetSummary, FindingSummary } from "../api/types";

export function formatProjectModelCost(cost: number | undefined): string {
  if (cost === undefined) return "—";
  const precision = cost > 0 && cost < 0.0001 ? 6 : cost > 0 && cost < 0.01 ? 4 : 2;
  const scale = 10 ** precision;
  return `$${(Math.round(cost * scale) / scale).toFixed(precision)}`;
}

/** The same Core-owned totals in the project dashboard and the chat snapshot. */
export function ProjectSummaryCards({ assets, findings, run }: {
  assets: AssetSummary[];
  findings: FindingSummary[];
  run?: AgentRunSummary;
}) {
  const validated = findings.filter(finding => ["validated", "confirmed"].includes(finding.status)).length;
  const priority = findings.filter(finding => finding.severity === "critical" || finding.severity === "high").length;
  const missionStatus = run?.status.replace("_", " ") ?? "—";
  return <section className="metric-grid" aria-label="Project summary">
    <article className="metric-card accent-blue">
      <span className="metric-icon"><Target size={19} aria-hidden="true" /></span>
      <div><small>Assets</small><strong>{assets.length}</strong><span>In this project</span></div>
    </article>
    <article className="metric-card accent-violet">
      <span className="metric-icon"><Bot size={19} aria-hidden="true" /></span>
      <div><small>Mission</small><strong>{missionStatus}</strong><span title={run?.title ?? "No active run"}>{run?.title ?? "No active run"}</span></div>
    </article>
    <article className="metric-card accent-red">
      <span className="metric-icon"><FileCheck2 size={19} aria-hidden="true" /></span>
      <div><small>Findings</small><strong>{validated}</strong><span>{findings.length} total · {priority} priority</span></div>
    </article>
    <article className="metric-card accent-green">
      <span className="metric-icon"><DollarSign size={19} aria-hidden="true" /></span>
      <div><small>Model cost</small><strong>{formatProjectModelCost(run?.spentUsd)}</strong><span>Recorded for this mission</span></div>
    </article>
  </section>;
}
