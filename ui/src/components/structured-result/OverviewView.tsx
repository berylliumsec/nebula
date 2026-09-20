import { useState } from "react";
import { Table2, Share2 } from "lucide-react";
import type { PresentationPlan } from "./hints";
import { isScalar, typeLabel, type NormalizedResult, type ResultNode } from "./normalize";
import { fieldLabel, PropertyGrid, type NodeSelection } from "./PropertyGrid";
import { ScalarValue } from "./ValueView";

interface OverviewViewProps extends NodeSelection {
  result: NormalizedResult;
  plan: PresentationPlan;
  onOpenTable: (path: string) => void;
  onOpenGraph: (id: string) => void;
}

/**
 * What the result leads with: its top-level scalars, then the size of every
 * collection it carries. The order is presentation, not meaning — a field
 * called `status` is shown early because operators look there first, not
 * because Nebula decided anything about the result.
 */
export function OverviewView({ result, plan, onOpenTable, onOpenGraph, selected, onSelect }: OverviewViewProps) {
  const [showHidden, setShowHidden] = useState(false);
  const { analysis } = plan;
  const root = analysis.root;

  if (isScalar(root.type)) {
    return <section className="structured-overview">
      <p className="structured-derived">This result is a single {root.type} value.</p>
      <p className="structured-inspector-value"><ScalarValue value={root.value} /></p>
    </section>;
  }

  if (root.type === "unsupported") {
    return <section className="structured-overview">
      <p className="structured-empty">{root.note ?? "This value is not JSON-compatible."} The tree and raw views still show what was published.</p>
    </section>;
  }

  return <section className="structured-overview">
    {analysis.shape === "empty" && <p className="structured-empty">
      This result is an empty {root.type}. That is what the producer published; nothing was dropped.
    </p>}

    {analysis.overviewFields.length > 0 && <>
      <h3>Values</h3>
      <PropertyGrid result={result} node={root} plan={plan} entries={analysis.overviewFields} selected={selected} onSelect={onSelect} />
    </>}

    {plan.hiddenOverviewFields.length > 0 && <>
      <button type="button" className="button quiet" aria-expanded={showHidden} onClick={() => setShowHidden(!showHidden)}>
        {showHidden ? "Hide" : "Show"} {plan.hiddenOverviewFields.length} field{plan.hiddenOverviewFields.length === 1 ? "" : "s"} the producer hid by default
      </button>
      {showHidden && <PropertyGrid result={result} node={root} plan={plan} entries={plan.hiddenOverviewFields} selected={selected} onSelect={onSelect} />}
    </>}

    {analysis.collections.length > 0 && <>
      <h3>Collections</h3>
      <ul className="structured-collection-list">
        {analysis.collections.map((child: ResultNode) => {
          const table = analysis.tables.find((candidate) => candidate.path === child.path);
          const graph = analysis.graphs.find((candidate) => candidate.edgesPath === child.path || candidate.nodesPath === child.path);
          return <li key={child.path}>
            <button type="button" className="structured-collection" onClick={(event) => onSelect(child, event.currentTarget)}>
              <strong>{fieldLabel(plan, child)}</strong>
              <small>{typeLabel(child)}</small>
            </button>
            {table && <button type="button" className="button quiet" onClick={() => onOpenTable(table.path)}>
              <Table2 size={14} aria-hidden="true" /> Open as table
            </button>}
            {graph && <button type="button" className="button quiet" onClick={() => onOpenGraph(graph.id)}>
              <Share2 size={14} aria-hidden="true" /> Open relationships
            </button>}
          </li>;
        })}
      </ul>
    </>}

    {analysis.overviewFields.length === 0 && analysis.collections.length === 0 && analysis.shape !== "empty" && <p className="structured-empty">
      This result has no top-level values to lead with. The tree shows it in full.
    </p>}
  </section>;
}
