import { useMemo, useState } from "react";
import type { GraphCandidate } from "./analyze";
import { copyText } from "./format";
import type { PresentationPlan } from "./hints";
import { pathSegments, type NormalizedResult, type ResultNode } from "./normalize";
import type { NodeSelection } from "./PropertyGrid";
import { CopyAction } from "./ValueView";

/** Markers drawn before the graph says it is showing a subset. */
const NODE_LIMIT = 300;
const EDGE_LIMIT = 800;
const SIZE = 720;
const RADIUS = 250;

interface LaidOutNode {
  id: string;
  label: string;
  x: number;
  y: number;
  node?: ResultNode;
  /** True when this marker exists only because an edge names it. */
  implied: boolean;
}

interface LaidOutEdge {
  key: string;
  source: LaidOutNode;
  target: LaidOutNode;
  node: ResultNode;
}

interface GraphLayout {
  nodes: LaidOutNode[];
  edges: LaidOutEdge[];
  truncatedNodes: boolean;
  truncatedEdges: boolean;
}

function endpointText(value: unknown): string | undefined {
  if (typeof value === "string" || typeof value === "number") return String(value);
  return undefined;
}

/**
 * Deterministic circular placement. It runs only when the relationship view is
 * opened, and it never invents an edge: markers come from the published node
 * collection, plus any endpoint an edge names that the collection does not.
 */
export function layoutGraph(result: NormalizedResult, graph: GraphCandidate): GraphLayout {
  const byId = new Map<string, LaidOutNode>();
  const order: LaidOutNode[] = [];
  const add = (id: string, label: string, node: ResultNode | undefined, implied: boolean) => {
    const existing = byId.get(id);
    if (existing) {
      if (existing.implied && !implied) {
        existing.implied = false;
        existing.label = label;
        existing.node = node;
      }
      return existing;
    }
    const created: LaidOutNode = { id, label, x: 0, y: 0, node, implied };
    byId.set(id, created);
    order.push(created);
    return created;
  };

  const nodesNode = graph.nodesPath ? result.resolve(pathSegments(graph.nodesPath)) : undefined;
  if (nodesNode && nodesNode.type === "array" && graph.nodeIdKey) {
    for (const item of result.childWindow(nodesNode, 0, NODE_LIMIT)) {
      if (item.type !== "object") continue;
      const record = item.value as Record<string, unknown>;
      const id = endpointText(record[graph.nodeIdKey]);
      if (id === undefined) continue;
      const label = endpointText(record[graph.nodeLabelKey ?? graph.nodeIdKey]) ?? id;
      add(id, label, item, false);
    }
  }

  const edgesNode = result.resolve(pathSegments(graph.edgesPath));
  const edges: LaidOutEdge[] = [];
  if (edgesNode && edgesNode.type === "array") {
    for (const item of result.childWindow(edgesNode, 0, EDGE_LIMIT)) {
      if (item.type !== "object") continue;
      const record = item.value as Record<string, unknown>;
      const source = endpointText(record[graph.sourceKey]);
      const target = endpointText(record[graph.targetKey]);
      if (source === undefined || target === undefined) continue;
      if (!byId.has(source) && order.length >= NODE_LIMIT) continue;
      if (!byId.has(target) && order.length >= NODE_LIMIT) continue;
      edges.push({
        key: item.path,
        source: add(source, source, undefined, true),
        target: add(target, target, undefined, true),
        node: item,
      });
    }
  }

  const count = Math.max(1, order.length);
  order.forEach((node, index) => {
    const angle = (index / count) * Math.PI * 2 - Math.PI / 2;
    node.x = SIZE / 2 + RADIUS * Math.cos(angle);
    node.y = SIZE / 2 + RADIUS * Math.sin(angle);
  });

  return {
    nodes: order,
    edges,
    truncatedNodes: graph.nodeCount > order.length && graph.nodeCount > NODE_LIMIT,
    truncatedEdges: graph.edgeCount > edges.length,
  };
}

interface GraphViewProps extends NodeSelection {
  result: NormalizedResult;
  plan: PresentationPlan;
  graph: GraphCandidate;
}

/**
 * A node-and-edge view of a relationship-shaped collection, with the same
 * relationships listed as a table underneath. The table is not a fallback for
 * failure: it is the representation for anyone not using the picture.
 */
export function GraphView({ result, plan, graph, selected, onSelect }: GraphViewProps) {
  const [showGraphic, setShowGraphic] = useState(true);
  const layout = useMemo(() => layoutGraph(result, graph), [graph, result]);
  const implied = layout.nodes.filter((node) => node.implied).length;

  return <div className="structured-graph">
    <p className="structured-derived">
      Derived presentation: {layout.nodes.length.toLocaleString()} {layout.nodes.length === 1 ? "node" : "nodes"} and {layout.edges.length.toLocaleString()} {layout.edges.length === 1 ? "relationship" : "relationships"} read from
      {" "}<code className="structured-mono">{graph.edgesPath}</code>
      {graph.nodesPath ? <> and <code className="structured-mono">{graph.nodesPath}</code></> : <> (the nodes are the endpoints these relationships name)</>}
      {" "}using <code className="structured-mono">{graph.sourceKey}</code> → <code className="structured-mono">{graph.targetKey}</code>.
    </p>
    {implied > 0 && graph.nodesPath && <p className="structured-derived" role="status">
      {implied} {implied === 1 ? "endpoint names a node" : "endpoints name nodes"} that the published node collection does not contain. They are shown as named endpoints, not invented records.
    </p>}
    {(layout.truncatedNodes || layout.truncatedEdges) && <p className="structured-derived" role="status">
      The picture is limited to {NODE_LIMIT.toLocaleString()} nodes and {EDGE_LIMIT.toLocaleString()} relationships. The table below and the tree reach every one.
    </p>}

    <button type="button" className="button quiet" aria-expanded={showGraphic} onClick={() => setShowGraphic(!showGraphic)}>
      {showGraphic ? "Hide the picture" : "Show the picture"}
    </button>

    {showGraphic && <svg
      className="structured-graph-canvas"
      viewBox={`0 0 ${SIZE} ${SIZE}`}
      role="group"
      aria-label={`Relationship graph: ${layout.nodes.length} nodes, ${layout.edges.length} relationships`}
    >
      <g className="structured-graph-edges">
        {layout.edges.map((edge) => <line
          key={edge.key}
          x1={edge.source.x}
          y1={edge.source.y}
          x2={edge.target.x}
          y2={edge.target.y}
        />)}
      </g>
      {layout.nodes.map((node) => <g
        key={node.id}
        className="structured-graph-node"
        data-implied={node.implied ? "true" : undefined}
        data-selected={node.node && selected?.path === node.node.path ? "true" : undefined}
        role="button"
        tabIndex={0}
        aria-label={`${node.label}${node.implied ? " (named by a relationship, not in the node collection)" : ""}`}
        onClick={() => node.node && onSelect(node.node)}
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          if (node.node) onSelect(node.node);
        }}
      >
        <circle cx={node.x} cy={node.y} r={node.implied ? 7 : 10} />
        <text x={node.x} y={node.y - 16} textAnchor="middle">{node.label.slice(0, 24)}</text>
      </g>)}
    </svg>}

    <table className="structured-graph-table">
      <caption>Relationships in {graph.label}</caption>
      <thead>
        <tr>
          <th scope="col">{graph.sourceKey}</th>
          <th scope="col">{graph.targetKey}</th>
          <th scope="col"><span className="sr-only">Relationship detail</span></th>
        </tr>
      </thead>
      <tbody>
        {layout.edges.map((edge) => <tr key={edge.key} data-selected={selected?.path === edge.node.path ? "true" : undefined}>
          <td>{edge.source.label}</td>
          <td>{edge.target.label}</td>
          <td className="structured-table-actions">
            <CopyAction text={copyText(edge.node.value)} label={`Copy relationship ${edge.source.label} to ${edge.target.label}`} />
            <button type="button" className="structured-nested" onClick={(event) => onSelect(edge.node, event.currentTarget)}>
              Detail<span className="sr-only"> of {edge.source.label} to {edge.target.label}</span>
            </button>
          </td>
        </tr>)}
        {layout.edges.length === 0 && <tr><td colSpan={3}><p className="structured-empty">No relationship in this collection names both endpoints.</p></td></tr>}
      </tbody>
    </table>
    {plan.descriptions[graph.edgesPath] && <p>{plan.descriptions[graph.edgesPath]}</p>}
  </div>;
}

export default GraphView;
