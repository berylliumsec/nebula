import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";
import { ChevronRight, PanelRight, Search } from "lucide-react";
import { IconAction } from "../IconAction";
import { compactValue, copyText } from "./format";
import type { PresentationPlan } from "./hints";
import { isScalar, typeLabel, type NormalizedResult, type ResultNode } from "./normalize";
import { fieldLabel, isRedacted, type NodeSelection } from "./PropertyGrid";
import { CopyAction, ScalarValue } from "./ValueView";

/** Children rendered per container before a "show more" row takes over. */
const CHILD_PAGE = 100;
/** Nodes visited per search slice, so a long scan never blocks typing. */
const SEARCH_SLICE = 2_000;
/** Upper bound on one search, reported to the operator when it is reached. */
const SEARCH_LIMIT = 50_000;
/** Children examined per container while a search filters the tree. */
const SEARCH_SCAN_CAP = 5_000;

interface TreeSearch {
  matches: Set<string> | undefined;
  visible: Set<string> | undefined;
  scanning: boolean;
  truncated: boolean;
  count: number;
}

/**
 * Search property names and values without freezing the interface.
 *
 * The walk runs in slices and every effect cleanup cancels the run in flight,
 * so a new keystroke abandons the previous search instead of racing it.
 */
export function useTreeSearch(result: NormalizedResult, query: string): TreeSearch {
  const [state, setState] = useState<TreeSearch>({ matches: undefined, visible: undefined, scanning: false, truncated: false, count: 0 });
  useEffect(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) {
      setState({ matches: undefined, visible: undefined, scanning: false, truncated: false, count: 0 });
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const matches = new Set<string>();
    const visible = new Set<string>();
    const queue: ResultNode[] = [result.root];
    let cursor = 0;
    let scanned = 0;
    setState({ matches: undefined, visible: undefined, scanning: true, truncated: false, count: 0 });

    const record = (node: ResultNode) => {
      matches.add(node.path);
      let path: string | undefined = node.path;
      while (path) {
        visible.add(path);
        path = result.nodeAt(path)?.parentPath;
      }
    };

    const slice = () => {
      if (cancelled) return;
      let steps = 0;
      while (cursor < queue.length && steps < SEARCH_SLICE && scanned < SEARCH_LIMIT) {
        const node = queue[cursor];
        cursor += 1;
        steps += 1;
        scanned += 1;
        const name = node.key === undefined ? "" : String(node.key);
        const text = isScalar(node.type) ? String(node.value) : "";
        if (name.toLowerCase().includes(needle) || text.toLowerCase().includes(needle)) record(node);
        if (node.type === "object" || node.type === "array") {
          for (const child of result.childWindow(node, 0, SEARCH_LIMIT)) queue.push(child);
        }
      }
      const done = cursor >= queue.length || scanned >= SEARCH_LIMIT;
      setState({
        matches: new Set(matches),
        visible: new Set(visible),
        scanning: !done,
        truncated: scanned >= SEARCH_LIMIT && cursor < queue.length,
        count: matches.size,
      });
      if (!done) timer = window.setTimeout(slice, 0);
    };
    timer = window.setTimeout(slice, 0);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [query, result]);
  return state;
}

interface TreeRow {
  node: ResultNode;
  depth: number;
  hasChildren: boolean;
  expanded: boolean;
  /** Children currently rendered, and how many this container can show. */
  shown: number;
  available: number;
}

interface TreeViewProps extends NodeSelection {
  result: NormalizedResult;
  plan: PresentationPlan;
  /** Paths expanded on first render, e.g. the path the inspector came from. */
  initialExpanded?: string[];
}

/**
 * The fallback that always works: every value in the result, in place, with
 * its name, runtime type, collection size and full path. Children are rendered
 * a page at a time so a wide container cannot flood the DOM.
 */
export function TreeView({ result, plan, selected, onSelect, initialExpanded = [] }: TreeViewProps) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(["$", ...initialExpanded]));
  const [shown, setShown] = useState<Record<string, number>>({});
  const [query, setQuery] = useState("");
  const [focused, setFocused] = useState<string>("$");
  const listRef = useRef<HTMLUListElement>(null);
  const search = useTreeSearch(result, query);

  useEffect(() => {
    for (const path of initialExpanded) {
      setExpanded((current) => (current.has(path) ? current : new Set([...current, path])));
    }
  }, [initialExpanded]);

  const toggle = useCallback((path: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const rows = useMemo(() => {
    const collected: TreeRow[] = [];
    const walk = (node: ResultNode, depth: number) => {
      const hasChildren = result.hasChildren(node);
      const open = expanded.has(node.path) || (search.visible !== undefined && search.visible.has(node.path));
      const limit = shown[node.path] ?? CHILD_PAGE;
      const available = search.visible === undefined
        ? result.childKeys(node).length
        : result.childWindow(node, 0, SEARCH_SCAN_CAP).filter((child) => search.visible!.has(child.path)).length;
      collected.push({ node, depth, hasChildren, expanded: hasChildren && open, shown: Math.min(limit, available), available });
      if (!hasChildren || !open) return;
      const children = search.visible === undefined
        ? result.childWindow(node, 0, limit)
        : result.childWindow(node, 0, SEARCH_SCAN_CAP).filter((child) => search.visible!.has(child.path)).slice(0, limit);
      for (const child of children) walk(child, depth + 1);
    };
    walk(result.root, 0);
    return collected;
  }, [expanded, result, search.visible, shown]);

  const move = (event: KeyboardEvent<HTMLUListElement>) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const buttons = [...(listRef.current?.querySelectorAll<HTMLButtonElement>("[data-tree-row]") ?? [])];
    const current = buttons.findIndex((button) => button === document.activeElement);
    if (current < 0 && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    const next = event.key === "Home" ? 0
      : event.key === "End" ? buttons.length - 1
      : event.key === "ArrowDown" ? Math.min(buttons.length - 1, current + 1)
      : Math.max(0, current - 1);
    buttons[next]?.focus();
    setFocused(buttons[next]?.dataset.treeRow ?? focused);
  };

  return <div className="structured-tree">
    <label className="structured-search">
      <Search size={15} aria-hidden="true" />
      <span className="sr-only">Search property names and values</span>
      <input type="search" value={query} placeholder="Search names and values" onChange={(event) => setQuery(event.target.value)} />
    </label>
    {query.trim() && <p className="structured-derived" role="status">
      {search.scanning ? "Searching…" : `${search.count} ${search.count === 1 ? "match" : "matches"}`}
      {search.truncated && ` · stopped after ${SEARCH_LIMIT.toLocaleString()} values; narrow the search to reach the rest`}
    </p>}
    <ul className="structured-tree-list" ref={listRef} onKeyDown={move}>
      {rows.map((row) => {
        const label = row.node.key === undefined ? "Result" : fieldLabel(plan, row.node);
        const matched = search.matches?.has(row.node.path);
        return <li key={row.node.path} style={{ "--structured-depth": row.depth } as CSSProperties}>
          <div className="structured-tree-row" data-selected={selected?.path === row.node.path ? "true" : undefined} data-match={matched ? "true" : undefined}>
            <button
              type="button"
              data-tree-row={row.node.path}
              className="structured-tree-toggle"
              aria-expanded={row.hasChildren ? row.expanded : undefined}
              tabIndex={focused === row.node.path ? 0 : -1}
              onFocus={() => setFocused(row.node.path)}
              onClick={() => (row.hasChildren ? toggle(row.node.path) : onSelect(row.node))}
              onKeyDown={(event) => {
                if (event.key === "ArrowRight" && row.hasChildren && !row.expanded) { event.preventDefault(); toggle(row.node.path); }
                if (event.key === "ArrowLeft" && row.expanded) { event.preventDefault(); toggle(row.node.path); }
              }}
            >
              {row.hasChildren
                ? <ChevronRight size={14} aria-hidden="true" className={row.expanded ? "structured-chevron open" : "structured-chevron"} />
                : <span className="structured-chevron" aria-hidden="true" />}
              <span className="structured-tree-name">{label}</span>
              <span className="structured-tree-type">{typeLabel(row.node)}</span>
              {isScalar(row.node.type)
                ? <span className="structured-tree-value">{String(row.node.value ?? "").includes("\n")
                    ? <span className="structured-mono">{compactValue(String(row.node.value).replace(/\n/g, " ⏎ "))}</span>
                    : <ScalarValue name={row.node.key} value={row.node.value} format={plan.formats[String(row.node.key)]} redacted={isRedacted(plan, row.node)} />}</span>
                : <span className="structured-tree-value structured-mono">{compactValue(row.node.value)}</span>}
              <span className="sr-only">{row.node.path}</span>
            </button>
            <span className="structured-tree-actions">
              <CopyAction text={row.node.path} label={`Copy path of ${label}`} />
              <CopyAction text={copyText(row.node.value)} label={`Copy value of ${label}`} />
              <IconAction icon={PanelRight} label={`Inspect ${label}`} onClick={(event) => onSelect(row.node, event.currentTarget)} />
            </span>
          </div>
          {row.expanded && row.shown < row.available && <button
            type="button"
            className="button quiet structured-more"
            onClick={() => setShown({ ...shown, [row.node.path]: row.shown + CHILD_PAGE })}
          >
            Show more of {label} ({row.shown.toLocaleString()} of {row.available.toLocaleString()})
          </button>}
        </li>;
      })}
    </ul>
  </div>;
}
