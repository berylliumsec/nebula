/**
 * Normalizer: turn any JSON-compatible value into a stable node view of itself.
 *
 * Nodes are created on demand. A result with a hundred thousand rows must cost
 * the same to open as one with three, so nothing below the requested window is
 * materialised, and every node keeps a reference to the original value rather
 * than a copy of it. The input is never rewritten: this layer only describes.
 */

export type RuntimeType =
  | "object"
  | "array"
  | "string"
  | "number"
  | "boolean"
  | "null"
  | "unsupported";

export interface ResultNode {
  /** Display and copy form of this node's location, e.g. `$.hosts[0].name`. */
  readonly path: string;
  readonly segments: readonly (string | number)[];
  readonly parentPath: string | undefined;
  readonly key: string | number | undefined;
  readonly type: RuntimeType;
  /** The original value, untouched. */
  readonly value: unknown;
  /** Entries, items or characters; 0 where size means nothing. */
  readonly size: number;
  readonly depth: number;
  /** Why a value is unsupported or unreachable, when it is. */
  readonly note?: string;
  /** Ancestor containers, used to refuse to walk a structure into itself. */
  readonly ancestors: readonly object[];
}

const IDENTIFIER = /^[A-Za-z_$][A-Za-z0-9_$]*$/;

/** Append one segment to a path in a form an operator can paste back. */
export function joinPath(path: string, segment: string | number): string {
  if (typeof segment === "number") return `${path}[${segment}]`;
  if (IDENTIFIER.test(segment)) return `${path}.${segment}`;
  return `${path}[${JSON.stringify(segment)}]`;
}

export function formatPath(segments: readonly (string | number)[]): string {
  return segments.reduce<string>((path, segment) => joinPath(path, segment), "$");
}

/**
 * Split a path back into segments. It is the exact inverse of `joinPath`, so a
 * quoted key containing a dot or a bracket round-trips unchanged.
 */
export function pathSegments(path: string): (string | number)[] {
  const segments: (string | number)[] = [];
  let index = path.startsWith("$") ? 1 : 0;
  while (index < path.length) {
    const character = path[index];
    if (character === ".") {
      index += 1;
      const start = index;
      while (index < path.length && path[index] !== "." && path[index] !== "[") index += 1;
      if (index > start) segments.push(path.slice(start, index));
      continue;
    }
    if (character === "[") {
      index += 1;
      if (path[index] === '"') {
        let quoted = '"';
        index += 1;
        while (index < path.length && path[index] !== '"') {
          if (path[index] === "\\") {
            quoted += path[index];
            index += 1;
          }
          quoted += path[index];
          index += 1;
        }
        quoted += '"';
        index += 1;
        try {
          segments.push(JSON.parse(quoted) as string);
        } catch {
          // diagnostic-expected: a malformed path segment is used verbatim.
          segments.push(quoted.slice(1, -1));
        }
      } else {
        const start = index;
        while (index < path.length && path[index] !== "]") index += 1;
        const token = path.slice(start, index);
        segments.push(/^\d+$/.test(token) ? Number(token) : token);
      }
      if (path[index] === "]") index += 1;
      continue;
    }
    // A bare leading segment, as in `hosts[0]` without the `$.` prefix.
    const start = index;
    while (index < path.length && path[index] !== "." && path[index] !== "[") index += 1;
    if (index > start) segments.push(path.slice(start, index));
  }
  return segments;
}

function plainObject(value: object): boolean {
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

export function runtimeTypeOf(value: unknown): RuntimeType {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  switch (typeof value) {
    case "string":
      return "string";
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "object":
      return plainObject(value as object) ? "object" : "unsupported";
    default:
      return "unsupported";
  }
}

function unsupportedNote(value: unknown): string | undefined {
  if (value === undefined) return "undefined is not part of JSON";
  const kind = typeof value;
  if (kind === "function") return "a function is not part of JSON";
  if (kind === "symbol") return "a symbol is not part of JSON";
  if (kind === "bigint") return "a bigint is not part of JSON";
  if (value && kind === "object") {
    const name = (value as object).constructor?.name;
    return `${name ?? "a non-plain object"} is not part of JSON`;
  }
  return undefined;
}

export function sizeOf(value: unknown, type: RuntimeType): number {
  if (type === "array") return (value as unknown[]).length;
  if (type === "object") return Object.keys(value as object).length;
  if (type === "string") return (value as string).length;
  return 0;
}

/** "object · 3 properties" — type first, then the count that makes it concrete. */
export function typeLabel(node: ResultNode): string {
  if (node.type === "array") return `array · ${node.size} ${node.size === 1 ? "item" : "items"}`;
  if (node.type === "object") return `object · ${node.size} ${node.size === 1 ? "property" : "properties"}`;
  if (node.type === "string") return `string · ${node.size} ${node.size === 1 ? "character" : "characters"}`;
  if (node.type === "unsupported") return node.note ?? "unsupported value";
  return node.type;
}

function makeNode(
  value: unknown,
  segments: readonly (string | number)[],
  parentPath: string | undefined,
  depth: number,
  ancestors: readonly object[],
): ResultNode {
  const type = runtimeTypeOf(value);
  const cycle = typeof value === "object" && value !== null && ancestors.includes(value as object);
  return {
    path: formatPath(segments),
    segments,
    parentPath,
    key: segments.length ? segments[segments.length - 1] : undefined,
    type: cycle ? "unsupported" : type,
    value,
    size: cycle ? 0 : sizeOf(value, type),
    depth,
    note: cycle ? "this value refers back to an ancestor" : type === "unsupported" ? unsupportedNote(value) : undefined,
    ancestors,
  };
}

/** A normalized view over one authoritative result value. */
export class NormalizedResult {
  readonly root: ResultNode;
  private readonly nodes = new Map<string, ResultNode>();

  constructor(value: unknown) {
    this.root = makeNode(value, [], undefined, 0, []);
    this.nodes.set(this.root.path, this.root);
  }

  /** Child keys in their original order; cheap, and never materialises a child. */
  childKeys(node: ResultNode): (string | number)[] {
    if (node.note) return [];
    if (node.type === "array") return (node.value as unknown[]).map((_item, index) => index);
    if (node.type === "object") return Object.keys(node.value as object);
    return [];
  }

  hasChildren(node: ResultNode): boolean {
    return !node.note && (node.type === "array" || node.type === "object") && node.size > 0;
  }

  childAt(node: ResultNode, key: string | number): ResultNode {
    const segments = [...node.segments, key];
    const path = formatPath(segments);
    const existing = this.nodes.get(path);
    if (existing) return existing;
    const container = node.value as Record<string | number, unknown>;
    const child = makeNode(container[key], segments, node.path, node.depth + 1, [
      ...node.ancestors,
      node.value as object,
    ]);
    this.nodes.set(path, child);
    return child;
  }

  /** A bounded slice of a node's children, so large collections stay cheap. */
  childWindow(node: ResultNode, offset = 0, limit = Number.POSITIVE_INFINITY): ResultNode[] {
    const keys = this.childKeys(node);
    const end = limit === Number.POSITIVE_INFINITY ? keys.length : Math.min(keys.length, offset + limit);
    const window: ResultNode[] = [];
    for (let index = Math.max(0, offset); index < end; index += 1) window.push(this.childAt(node, keys[index]));
    return window;
  }

  children(node: ResultNode): ResultNode[] {
    return this.childWindow(node);
  }

  /** Resolve a path produced by this normalizer back to its node. */
  resolve(segments: readonly (string | number)[]): ResultNode | undefined {
    let node = this.root;
    for (const segment of segments) {
      if (!this.hasChildren(node)) return undefined;
      const keys = this.childKeys(node);
      const match = keys.find((key) => String(key) === String(segment));
      if (match === undefined) return undefined;
      node = this.childAt(node, match);
    }
    return node;
  }

  nodeAt(path: string): ResultNode | undefined {
    return this.nodes.get(path);
  }
}

/** Scalars are the values an overview or a table cell can show directly. */
export function isScalar(type: RuntimeType): boolean {
  return type === "string" || type === "number" || type === "boolean" || type === "null";
}
