/**
 * Presentation-hint adapter.
 *
 * Hints are advice, never a prerequisite. Each field is validated on its own:
 * a malformed field is dropped with a reason an operator can read, and the
 * rest of the document still applies. A hints document that is entirely
 * invalid leaves the result rendering exactly as it would with no hints.
 *
 * Hints never hide anything permanently. A field the producer hid by default
 * is still one control away, and a redacted field is masked with an explicit
 * reveal rather than removed.
 */

import type { ResultAnalysis, TableCandidate } from "./analyze";
import { FORMAT_HINTS, type FormatHint } from "./format";
import { pathSegments, type NormalizedResult, type ResultNode } from "./normalize";

export interface PresentationHints {
  titleField?: string;
  summaryField?: string;
  fieldOrder?: string[];
  hiddenFields?: string[];
  labels?: Record<string, string>;
  descriptions?: Record<string, string>;
  /** Table path (or `*` for every table) to the columns shown by default. */
  tableColumns?: Record<string, string[]>;
  graph?: {
    nodesPath?: string;
    edgesPath?: string;
    sourceKey?: string;
    targetKey?: string;
    nodeIdKey?: string;
    nodeLabelKey?: string;
  };
  formats?: Record<string, FormatHint>;
  redactFields?: string[];
}

export interface PresentationPlan {
  analysis: ResultAnalysis;
  /** Overview fields a hint hid by default; still reachable in the interface. */
  hiddenOverviewFields: ResultNode[];
  /** Table path to the columns a hint hid by default. */
  hiddenColumns: Record<string, string[]>;
  labels: Record<string, string>;
  descriptions: Record<string, string>;
  formats: Record<string, FormatHint>;
  redacted: string[];
  /** Human-readable reasons for every hint that was not used. */
  ignored: string[];
  hinted: boolean;
}

const MAX_LIST = 200;

function stringList(value: unknown, field: string, ignored: string[]): string[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value)) {
    ignored.push(`${field} was ignored: it is not a list of field names.`);
    return undefined;
  }
  const items = value.filter((item): item is string => typeof item === "string" && item.length > 0);
  if (items.length !== value.length) ignored.push(`${field} ignored entries that were not field names.`);
  return items.slice(0, MAX_LIST);
}

function stringMap(value: unknown, field: string, ignored: string[]): Record<string, string> | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    ignored.push(`${field} was ignored: it is not an object of field names to text.`);
    return undefined;
  }
  const entries = Object.entries(value as Record<string, unknown>).filter(
    (entry): entry is [string, string] => typeof entry[1] === "string",
  );
  if (entries.length !== Object.keys(value as object).length) {
    ignored.push(`${field} ignored entries whose value was not text.`);
  }
  return Object.fromEntries(entries.slice(0, MAX_LIST));
}

function stringField(value: unknown, field: string, ignored: string[]): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !value) {
    ignored.push(`${field} was ignored: it is not a field name.`);
    return undefined;
  }
  return value;
}

/** Validate a hints document field by field. This never throws. */
export function readHints(raw: unknown): { hints: PresentationHints; ignored: string[] } {
  const ignored: string[] = [];
  if (raw === undefined || raw === null) return { hints: {}, ignored };
  if (typeof raw !== "object" || Array.isArray(raw)) {
    return { hints: {}, ignored: ["Presentation hints were ignored: they are not an object."] };
  }
  const source = raw as Record<string, unknown>;
  const hints: PresentationHints = {
    titleField: stringField(source.titleField, "titleField", ignored),
    summaryField: stringField(source.summaryField, "summaryField", ignored),
    fieldOrder: stringList(source.fieldOrder, "fieldOrder", ignored),
    hiddenFields: stringList(source.hiddenFields, "hiddenFields", ignored),
    labels: stringMap(source.labels, "labels", ignored),
    descriptions: stringMap(source.descriptions, "descriptions", ignored),
    redactFields: stringList(source.redactFields, "redactFields", ignored),
  };

  if (source.tableColumns !== undefined) {
    if (typeof source.tableColumns !== "object" || source.tableColumns === null || Array.isArray(source.tableColumns)) {
      ignored.push("tableColumns was ignored: it is not an object of table paths to column lists.");
    } else {
      const columns: Record<string, string[]> = {};
      for (const [path, value] of Object.entries(source.tableColumns as Record<string, unknown>)) {
        const list = stringList(value, `tableColumns[${path}]`, ignored);
        if (list && list.length > 0) columns[path] = list;
      }
      if (Object.keys(columns).length > 0) hints.tableColumns = columns;
    }
  }

  if (source.formats !== undefined) {
    const map = stringMap(source.formats, "formats", ignored);
    const formats: Record<string, FormatHint> = {};
    for (const [field, value] of Object.entries(map ?? {})) {
      if (FORMAT_HINTS.includes(value as FormatHint)) formats[field] = value as FormatHint;
      else ignored.push(`formats[${field}] was ignored: ${value} is not a known format.`);
    }
    if (Object.keys(formats).length > 0) hints.formats = formats;
  }

  if (source.graph !== undefined) {
    if (typeof source.graph !== "object" || source.graph === null || Array.isArray(source.graph)) {
      ignored.push("graph was ignored: it is not an object.");
    } else {
      const graph = source.graph as Record<string, unknown>;
      const mapping: NonNullable<PresentationHints["graph"]> = {
        nodesPath: stringField(graph.nodesPath, "graph.nodesPath", ignored),
        edgesPath: stringField(graph.edgesPath, "graph.edgesPath", ignored),
        sourceKey: stringField(graph.sourceKey, "graph.sourceKey", ignored),
        targetKey: stringField(graph.targetKey, "graph.targetKey", ignored),
        nodeIdKey: stringField(graph.nodeIdKey, "graph.nodeIdKey", ignored),
        nodeLabelKey: stringField(graph.nodeLabelKey, "graph.nodeLabelKey", ignored),
      };
      if (Object.values(mapping).some((value) => value !== undefined)) hints.graph = mapping;
    }
  }

  return { hints, ignored };
}

function orderedFields(fields: ResultNode[], hints: PresentationHints): ResultNode[] {
  const order = [
    ...(hints.titleField ? [hints.titleField] : []),
    ...(hints.summaryField ? [hints.summaryField] : []),
    ...(hints.fieldOrder ?? []),
  ];
  if (order.length === 0) return fields;
  const rank = new Map(order.map((name, index) => [name, index]));
  return [...fields].sort((left, right) => {
    const leftRank = rank.get(String(left.key)) ?? Number.MAX_SAFE_INTEGER;
    const rightRank = rank.get(String(right.key)) ?? Number.MAX_SAFE_INTEGER;
    if (leftRank !== rightRank) return leftRank - rightRank;
    return fields.indexOf(left) - fields.indexOf(right);
  });
}

function applyGraphHint(
  result: NormalizedResult,
  analysis: ResultAnalysis,
  hints: PresentationHints,
  ignored: string[],
): ResultAnalysis {
  const mapping = hints.graph;
  if (!mapping) return analysis;
  const edgesPath = mapping.edgesPath;
  if (!edgesPath) {
    // A partial mapping still refines a graph the analyzer already found.
    const graphs = analysis.graphs.map((graph) => ({
      ...graph,
      sourceKey: mapping.sourceKey ?? graph.sourceKey,
      targetKey: mapping.targetKey ?? graph.targetKey,
      nodeIdKey: mapping.nodeIdKey ?? graph.nodeIdKey,
      nodeLabelKey: mapping.nodeLabelKey ?? graph.nodeLabelKey,
    }));
    return { ...analysis, graphs };
  }
  const edges = findByPath(result, edgesPath);
  if (!edges || edges.type !== "array" || edges.size === 0) {
    ignored.push(`graph.edgesPath was ignored: ${edgesPath} is not a non-empty array in this result.`);
    return analysis;
  }
  const nodes = mapping.nodesPath ? findByPath(result, mapping.nodesPath) : undefined;
  if (mapping.nodesPath && (!nodes || nodes.type !== "array")) {
    ignored.push(`graph.nodesPath was ignored: ${mapping.nodesPath} is not an array in this result.`);
  }
  const existing = analysis.graphs.find((graph) => graph.edgesPath === edges.path);
  const hinted = {
    id: `hint:${edges.path}`,
    label: existing?.label ?? "Relationships",
    nodesPath: nodes && nodes.type === "array" ? nodes.path : existing?.nodesPath,
    edgesPath: edges.path,
    sourceKey: mapping.sourceKey ?? existing?.sourceKey ?? "source",
    targetKey: mapping.targetKey ?? existing?.targetKey ?? "target",
    nodeIdKey: mapping.nodeIdKey ?? existing?.nodeIdKey,
    nodeLabelKey: mapping.nodeLabelKey ?? existing?.nodeLabelKey,
    derivedNodes: !(nodes && nodes.type === "array") && !existing?.nodesPath,
    edgeCount: edges.size,
    nodeCount: nodes && nodes.type === "array" ? nodes.size : (existing?.nodeCount ?? 0),
    danglingEndpoints: existing?.danglingEndpoints ?? 0,
    sampled: existing?.sampled ?? false,
  };
  return {
    ...analysis,
    graphs: [hinted, ...analysis.graphs.filter((graph) => graph.edgesPath !== edges.path)],
  };
}

/** Resolve a hinted path against the result, accepting `$.a.b` and `a.b` alike. */
function findByPath(result: NormalizedResult, path: string): ResultNode | undefined {
  return result.resolve(pathSegments(path));
}

function tableColumnsFor(table: TableCandidate, hints: PresentationHints, ignored: string[]) {
  const requested = hints.tableColumns?.[table.path] ?? hints.tableColumns?.["*"];
  if (!requested) return { columns: table.columns, hidden: [] as string[] };
  const known = requested.filter((column) => table.columns.includes(column));
  const unknown = requested.filter((column) => !table.columns.includes(column));
  if (unknown.length > 0) {
    ignored.push(`tableColumns ignored ${unknown.join(", ")}: absent from the sampled rows of ${table.label}.`);
  }
  if (known.length === 0) return { columns: table.columns, hidden: [] as string[] };
  // Unlisted columns are hidden by default, not removed: the column menu and
  // the inspector still reach every field the rows carry.
  return { columns: [...known, ...table.columns.filter((column) => !known.includes(column))], hidden: table.columns.filter((column) => !known.includes(column)) };
}

/**
 * Build the presentation plan. With no hints this returns the analysis
 * untouched, which is the path every result renders through by default.
 */
export function buildPlan(
  result: NormalizedResult,
  analysis: ResultAnalysis,
  raw: unknown,
): PresentationPlan {
  const { hints, ignored } = readHints(raw);
  const hinted = Object.values(hints).some((value) => value !== undefined);
  const hiddenNames = new Set(hints.hiddenFields ?? []);
  const ordered = orderedFields(analysis.overviewFields, hints);
  const hiddenOverviewFields = ordered.filter((field) => hiddenNames.has(String(field.key)));
  const withGraph = applyGraphHint(result, analysis, hints, ignored);

  const hiddenColumns: Record<string, string[]> = {};
  const tables = withGraph.tables.map((table) => {
    const { columns, hidden } = tableColumnsFor(table, hints, ignored);
    if (hidden.length > 0) hiddenColumns[table.path] = hidden;
    return { ...table, columns };
  });

  return {
    analysis: {
      ...withGraph,
      tables,
      overviewFields: ordered.filter((field) => !hiddenNames.has(String(field.key))),
    },
    hiddenOverviewFields,
    hiddenColumns,
    labels: hints.labels ?? {},
    descriptions: hints.descriptions ?? {},
    formats: hints.formats ?? {},
    redacted: hints.redactFields ?? [],
    ignored,
    hinted,
  };
}
