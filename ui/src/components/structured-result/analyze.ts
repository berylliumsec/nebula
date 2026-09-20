/**
 * Analyzer: classify what a normalized result *is*, never what it means.
 *
 * Everything here is structural. A field called `status` earns a quieter badge
 * because of how it is shaped and named, not because Nebula decided the result
 * succeeded. Collections are classified from a bounded sample, so opening a
 * hundred-thousand-row result costs the same as opening a short one — every row
 * stays reachable in the table and the tree regardless of what was sampled.
 */

import { isScalar, type NormalizedResult, type ResultNode } from "./normalize";

/** How many items of a collection are inspected before classifying it. */
export const SAMPLE_LIMIT = 200;
/** How deep the scan looks for nested tables and graphs. */
const SCAN_DEPTH = 4;
/** A collection is treated as a table or a graph only past this share. */
const SHAPE_THRESHOLD = 0.6;

export interface TableCandidate {
  path: string;
  label: string;
  node: ResultNode;
  /** Union of keys across the sample, in first-seen order. */
  columns: string[];
  rowCount: number;
  /** True when columns came from a sample rather than every row. */
  sampled: boolean;
  homogeneity: number;
}

export interface GraphCandidate {
  id: string;
  label: string;
  nodesPath?: string;
  edgesPath: string;
  sourceKey: string;
  targetKey: string;
  nodeIdKey?: string;
  nodeLabelKey?: string;
  /** True when no node collection was published and endpoints imply the nodes. */
  derivedNodes: boolean;
  edgeCount: number;
  nodeCount: number;
  /** Endpoints in the sample that name no published node. */
  danglingEndpoints: number;
  sampled: boolean;
}

export type RootShape = "record" | "collection" | "scalar" | "empty" | "unsupported";

export interface ResultAnalysis {
  root: ResultNode;
  shape: RootShape;
  /** Prominent top-level scalars, presentation order. Presentation only. */
  overviewFields: ResultNode[];
  /** Top-level arrays and objects, for their counts. */
  collections: ResultNode[];
  tables: TableCandidate[];
  graphs: GraphCandidate[];
  /** True when any classification used a sample rather than every value. */
  sampled: boolean;
}

/**
 * Field names that usually carry the short human-readable part of a result.
 * Preferring them orders the overview; it never changes or interprets a value,
 * and a result without any of them still gets an overview.
 */
const PREFERRED_FIELDS = ["summary", "title", "name", "status", "result", "description"];

const NODE_CONTAINER_KEYS = ["nodes", "vertices"];
const EDGE_CONTAINER_KEYS = ["edges", "links", "relationships", "relations"];
const ENDPOINT_PAIRS: readonly (readonly [string, string])[] = [
  ["source", "target"],
  ["src", "dst"],
  ["from", "to"],
  ["source_id", "target_id"],
];
const NODE_ID_KEYS = ["id", "name", "key", "label"];
const NODE_LABEL_KEYS = ["label", "name", "title", "id"];

function sampleOf(result: NormalizedResult, node: ResultNode): ResultNode[] {
  return result.childWindow(node, 0, SAMPLE_LIMIT);
}

function objectShare(sample: ResultNode[]): number {
  if (sample.length === 0) return 0;
  return sample.filter((item) => item.type === "object").length / sample.length;
}

function columnUnion(sample: ResultNode[]): string[] {
  const columns: string[] = [];
  const seen = new Set<string>();
  for (const item of sample) {
    if (item.type !== "object") continue;
    for (const key of Object.keys(item.value as object)) {
      if (seen.has(key)) continue;
      seen.add(key);
      columns.push(key);
    }
  }
  return columns;
}

function labelForPath(node: ResultNode): string {
  if (node.segments.length === 0) return "Result";
  return node.segments.map((segment) => (typeof segment === "number" ? `#${segment}` : segment)).join(" › ");
}

function endpointPair(sample: ResultNode[]): readonly [string, string] | undefined {
  const objects = sample.filter((item) => item.type === "object");
  if (objects.length === 0) return undefined;
  for (const pair of ENDPOINT_PAIRS) {
    const matching = objects.filter((item) => {
      const record = item.value as Record<string, unknown>;
      return pair.every((key) => key in record && isScalar(scalarType(record[key])));
    }).length;
    if (matching / objects.length >= SHAPE_THRESHOLD) return pair;
  }
  return undefined;
}

function scalarType(value: unknown) {
  if (value === null) return "null" as const;
  const kind = typeof value;
  if (kind === "string") return "string" as const;
  if (kind === "number") return "number" as const;
  if (kind === "boolean") return "boolean" as const;
  return "object" as const;
}

function firstPresentKey(sample: ResultNode[], candidates: string[]): string | undefined {
  const objects = sample.filter((item) => item.type === "object");
  if (objects.length === 0) return undefined;
  return candidates.find((key) =>
    objects.filter((item) => key in (item.value as Record<string, unknown>)).length / objects.length >= SHAPE_THRESHOLD,
  );
}

function tableFor(result: NormalizedResult, node: ResultNode): TableCandidate | undefined {
  if (node.type !== "array" || node.size === 0) return undefined;
  const sample = sampleOf(result, node);
  const homogeneity = objectShare(sample);
  if (homogeneity < SHAPE_THRESHOLD) return undefined;
  const columns = columnUnion(sample);
  if (columns.length === 0) return undefined;
  return {
    path: node.path,
    label: labelForPath(node),
    node,
    columns,
    rowCount: node.size,
    sampled: node.size > sample.length,
    homogeneity,
  };
}

function graphFor(
  result: NormalizedResult,
  container: ResultNode,
  edges: ResultNode,
  nodes: ResultNode | undefined,
): GraphCandidate | undefined {
  const edgeSample = sampleOf(result, edges);
  const pair = endpointPair(edgeSample);
  if (!pair) return undefined;
  const nodeSample = nodes ? sampleOf(result, nodes) : [];
  const nodeIdKey = nodes ? firstPresentKey(nodeSample, NODE_ID_KEYS) : undefined;
  if (nodes && !nodeIdKey) return undefined;
  const identifiers = new Set(
    nodeSample.map((item) => String((item.value as Record<string, unknown>)[nodeIdKey ?? ""])),
  );
  let dangling = 0;
  if (nodes) {
    for (const edge of edgeSample) {
      if (edge.type !== "object") continue;
      const record = edge.value as Record<string, unknown>;
      for (const key of pair) {
        if (key in record && !identifiers.has(String(record[key]))) dangling += 1;
      }
    }
  }
  return {
    id: `${container.path}:${edges.path}`,
    label: labelForPath(container),
    nodesPath: nodes?.path,
    edgesPath: edges.path,
    sourceKey: pair[0],
    targetKey: pair[1],
    nodeIdKey,
    nodeLabelKey: nodes ? firstPresentKey(nodeSample, NODE_LABEL_KEYS) ?? nodeIdKey : undefined,
    derivedNodes: !nodes,
    edgeCount: edges.size,
    nodeCount: nodes ? nodes.size : 0,
    danglingEndpoints: dangling,
    sampled: edges.size > edgeSample.length || (nodes ? nodes.size > nodeSample.length : false),
  };
}

function rootShape(root: ResultNode): RootShape {
  if (root.type === "unsupported") return "unsupported";
  if (root.type === "object" || root.type === "array") return root.size === 0 ? "empty" : root.type === "array" ? "collection" : "record";
  return "scalar";
}

/** Classify a normalized result. The source values are only read, never changed. */
export function analyze(result: NormalizedResult): ResultAnalysis {
  const root = result.root;
  const tables: TableCandidate[] = [];
  const graphs: GraphCandidate[] = [];
  const claimedEdgePaths = new Set<string>();
  let sampled = false;

  // Breadth-first over containers only, bounded in depth and in fan-out, so a
  // wide result cannot turn opening the dashboard into a full traversal.
  const queue: ResultNode[] = [root];
  const containers: ResultNode[] = [];
  while (queue.length > 0 && containers.length < 200) {
    const node = queue.shift()!;
    if (node.type !== "object" && node.type !== "array") continue;
    containers.push(node);
    if (node.depth >= SCAN_DEPTH) continue;
    for (const child of result.childWindow(node, 0, node.type === "array" ? 1 : SAMPLE_LIMIT)) {
      if (child.type === "object" || child.type === "array") queue.push(child);
    }
  }

  for (const container of containers) {
    if (container.type !== "object") continue;
    const keys = result.childKeys(container).map(String);
    const nodesKey = NODE_CONTAINER_KEYS.find((key) => keys.includes(key));
    const edgesKey = EDGE_CONTAINER_KEYS.find((key) => keys.includes(key));
    if (!edgesKey) continue;
    const edges = result.childAt(container, edgesKey);
    const nodes = nodesKey ? result.childAt(container, nodesKey) : undefined;
    if (edges.type !== "array" || edges.size === 0) continue;
    if (nodes && nodes.type !== "array") continue;
    const candidate = graphFor(result, container, edges, nodes && nodes.size > 0 ? nodes : undefined);
    if (candidate) {
      graphs.push(candidate);
      claimedEdgePaths.add(edges.path);
    }
  }

  for (const container of containers) {
    const table = tableFor(result, container);
    if (table) {
      tables.push(table);
      sampled = sampled || table.sampled;
    }
    if (container.type !== "array" || claimedEdgePaths.has(container.path)) continue;
    const candidate = graphFor(result, container, container, undefined);
    if (candidate) {
      graphs.push(candidate);
      claimedEdgePaths.add(container.path);
    }
  }

  sampled = sampled || graphs.some((graph) => graph.sampled);

  const topLevel = root.type === "object" || root.type === "array" ? result.childWindow(root, 0, SAMPLE_LIMIT) : [];
  const scalars = topLevel.filter((child) => isScalar(child.type));
  const preferred = PREFERRED_FIELDS.flatMap((name) => scalars.filter((child) => String(child.key) === name));
  const overviewFields = [...preferred, ...scalars.filter((child) => !preferred.includes(child))];

  return {
    root,
    shape: rootShape(root),
    overviewFields,
    collections: topLevel.filter((child) => child.type === "object" || child.type === "array"),
    tables,
    graphs,
    sampled: sampled || root.size > topLevel.length,
  };
}
