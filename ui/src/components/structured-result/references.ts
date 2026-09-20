/**
 * References found by structure alone.
 *
 * A reference here means "this exact value also appears as an endpoint or an
 * identifier elsewhere in the same result". Nothing is inferred about what the
 * relationship means, and nothing is created: if the published result holds no
 * matching value, the inspector simply reports none.
 */

import type { ResultAnalysis } from "./analyze";
import { pathSegments, type NormalizedResult, type ResultNode } from "./normalize";

export interface StructuralReference {
  node: ResultNode;
  /** Why this node is shown: the key that matched, stated plainly. */
  reason: string;
}

const REFERENCE_LIMIT = 50;
const EDGE_SCAN_LIMIT = 500;

function identityOf(node: ResultNode, idKey: string | undefined): string | undefined {
  if (node.type === "string" || node.type === "number") return String(node.value);
  if (node.type === "object" && idKey) {
    const value = (node.value as Record<string, unknown>)[idKey];
    if (typeof value === "string" || typeof value === "number") return String(value);
  }
  return undefined;
}

/** Edges and nodes elsewhere in the result that carry this node's identity. */
export function structuralReferences(
  result: NormalizedResult,
  analysis: ResultAnalysis,
  node: ResultNode,
): StructuralReference[] {
  const references: StructuralReference[] = [];
  for (const graph of analysis.graphs) {
    if (references.length >= REFERENCE_LIMIT) break;
    const identity = identityOf(node, graph.nodeIdKey);
    if (identity === undefined) continue;
    const edges = result.resolve(pathSegments(graph.edgesPath));
    if (!edges || edges.type !== "array") continue;
    for (const edge of result.childWindow(edges, 0, EDGE_SCAN_LIMIT)) {
      if (references.length >= REFERENCE_LIMIT) break;
      if (edge.type !== "object" || edge.path === node.path) continue;
      const record = edge.value as Record<string, unknown>;
      for (const key of [graph.sourceKey, graph.targetKey]) {
        if (key in record && String(record[key]) === identity) {
          references.push({ node: edge, reason: `${key} is ${identity}` });
          break;
        }
      }
    }
    if (graph.nodesPath) {
      const nodes = result.resolve(pathSegments(graph.nodesPath));
      if (nodes && nodes.type === "array" && graph.nodeIdKey) {
        for (const candidate of result.childWindow(nodes, 0, EDGE_SCAN_LIMIT)) {
          if (references.length >= REFERENCE_LIMIT) break;
          if (candidate.path === node.path || candidate.type !== "object") continue;
          if (identityOf(candidate, graph.nodeIdKey) === identity) {
            references.push({ node: candidate, reason: `${graph.nodeIdKey} is ${identity}` });
          }
        }
      }
    }
  }
  return references;
}
