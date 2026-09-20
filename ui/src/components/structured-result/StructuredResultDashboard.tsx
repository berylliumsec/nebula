import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { FileJson, LayoutDashboard, ListTree, Share2, Table2 } from "lucide-react";
import { TabBar } from "../SurfacePrimitives";
import { analyze } from "./analyze";
import { buildPlan } from "./hints";
import { NormalizedResult, type ResultNode } from "./normalize";
import { InspectorPanel } from "./InspectorPanel";
import { OverviewView } from "./OverviewView";
import { RawView } from "./RawView";
import { TableView } from "./TableView";
import { TreeView } from "./TreeView";
import { ViewBoundary } from "./ViewBoundary";

// The relationship layout is only paid for by operators who open it.
const GraphView = lazy(() => import("./GraphView").then((module) => ({ default: module.GraphView })));

export type DashboardView = "overview" | "table" | "relationships" | "tree" | "raw";

export const DASHBOARD_VIEWS: DashboardView[] = ["overview", "table", "relationships", "tree", "raw"];

export function isDashboardView(value: string | null | undefined): value is DashboardView {
  return DASHBOARD_VIEWS.includes((value ?? "") as DashboardView);
}

interface StructuredResultDashboardProps {
  /** The authoritative published value. Every view is derived from it. */
  value: unknown;
  /** Optional presentation hints, kept separate from the value. */
  hints?: unknown;
  title: string;
  view?: DashboardView;
  onViewChange?: (view: DashboardView) => void;
  /** Tighter layout for the conversation drawer. */
  compact?: boolean;
}

/**
 * The schema-agnostic explorer.
 *
 * Nothing here knows any producer's types. A normalizer describes the value,
 * an analyzer classifies its shapes, an optional hint adapter refines the
 * presentation, and each renderer is guarded so that one failing view leaves
 * the others — and the unmodified result — reachable.
 */
export function StructuredResultDashboard({ value, hints, title, view, onViewChange, compact = false }: StructuredResultDashboardProps) {
  const result = useMemo(() => new NormalizedResult(value), [value]);
  const analysis = useMemo(() => analyze(result), [result]);
  const plan = useMemo(() => buildPlan(result, analysis, hints), [analysis, hints, result]);

  const [internalView, setInternalView] = useState<DashboardView>("overview");
  const [selected, setSelected] = useState<ResultNode>();
  const [tablePath, setTablePath] = useState<string>();
  const [graphId, setGraphId] = useState<string>();
  const [anchorPath, setAnchorPath] = useState<string>();
  const opener = useRef<HTMLElement | null>(null);
  const shell = useRef<HTMLDivElement>(null);
  const restoreFrame = useRef<number | undefined>(undefined);

  const active = view ?? internalView;
  const setView = useCallback((next: DashboardView) => {
    if (onViewChange) onViewChange(next);
    else setInternalView(next);
  }, [onViewChange]);

  const tables = plan.analysis.tables;
  const graphs = plan.analysis.graphs;
  const table = tables.find((candidate) => candidate.path === tablePath) ?? tables[0];
  const graph = graphs.find((candidate) => candidate.id === graphId) ?? graphs[0];

  const select = useCallback((node: ResultNode, from?: HTMLElement | null) => {
    opener.current = from ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    setSelected(node);
  }, []);

  const closeInspector = useCallback(() => {
    setSelected(undefined);
    // Focus is restored after the inspector has actually gone, so removing the
    // control that had focus cannot land the caret on the document body. Back
    // to whatever opened the inspector; if that control is gone — the view
    // changed while it was open — the selected tab takes focus instead.
    if (restoreFrame.current !== undefined) cancelAnimationFrame(restoreFrame.current);
    restoreFrame.current = requestAnimationFrame(() => {
      restoreFrame.current = undefined;
      const target = opener.current?.isConnected
        ? opener.current
        : shell.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]');
      target?.focus({ preventScroll: true });
    });
  }, []);

  useEffect(() => () => {
    if (restoreFrame.current !== undefined) cancelAnimationFrame(restoreFrame.current);
  }, []);

  const showInRaw = useCallback((node: ResultNode) => {
    setAnchorPath(node.path);
    setView("raw");
  }, [setView]);

  const available: DashboardView[] = ["overview", ...(tables.length > 0 ? ["table" as const] : []), ...(graphs.length > 0 ? ["relationships" as const] : []), "tree", "raw"];
  const current = available.includes(active) ? active : "overview";
  const icons: Record<DashboardView, ReactNode> = {
    overview: <LayoutDashboard size={16} />,
    table: <Table2 size={16} />,
    relationships: <Share2 size={16} />,
    tree: <ListTree size={16} />,
    raw: <FileJson size={16} />,
  };
  const labels: Record<DashboardView, string> = {
    overview: "Overview",
    table: tables.length > 1 ? `Tables (${tables.length})` : "Table",
    relationships: "Relationships",
    tree: "Tree",
    raw: "Raw",
  };

  return <div ref={shell} className={`structured-dashboard${compact ? " compact" : ""}`} data-view={current}>
    <TabBar
      className="structured-dashboard-tabs"
      label={`${title} views`}
      value={current}
      onChange={setView}
      items={available.map((item) => ({ id: item, label: labels[item], icon: icons[item] }))}
    />

    <div className="structured-dashboard-body">
      <div className="structured-dashboard-main">
        <ViewBoundary view={current} onFallback={() => setView("tree")}>
          {current === "overview" && <OverviewView
            result={result}
            plan={plan}
            selected={selected}
            onSelect={select}
            onOpenTable={(path) => { setTablePath(path); setView("table"); }}
            onOpenGraph={(id) => { setGraphId(id); setView("relationships"); }}
          />}

          {current === "table" && table && <>
            {tables.length > 1 && <label className="structured-picker">
              <span>Collection</span>
              <select value={table.path} onChange={(event) => setTablePath(event.target.value)}>
                {tables.map((candidate) => <option key={candidate.path} value={candidate.path}>
                  {candidate.label} · {candidate.rowCount.toLocaleString()} rows
                </option>)}
              </select>
            </label>}
            <TableView result={result} plan={plan} table={table} selected={selected} onSelect={select} />
          </>}

          {current === "relationships" && graph && <>
            {graphs.length > 1 && <label className="structured-picker">
              <span>Relationships</span>
              <select value={graph.id} onChange={(event) => setGraphId(event.target.value)}>
                {graphs.map((candidate) => <option key={candidate.id} value={candidate.id}>
                  {candidate.label} · {candidate.edgeCount.toLocaleString()} relationships
                </option>)}
              </select>
            </label>}
            <Suspense fallback={<p role="status">Preparing the relationship view…</p>}>
              <GraphView result={result} plan={plan} graph={graph} selected={selected} onSelect={select} />
            </Suspense>
          </>}

          {current === "tree" && <TreeView
            result={result}
            plan={plan}
            selected={selected}
            onSelect={select}
            initialExpanded={selected ? [selected.path] : []}
          />}

          {current === "raw" && <RawView value={value} title={title} anchorPath={anchorPath} />}
        </ViewBoundary>

        {plan.ignored.length > 0 && <details className="structured-hint-notice">
          <summary>{plan.ignored.length} presentation {plan.ignored.length === 1 ? "hint was" : "hints were"} not used</summary>
          <ul>{plan.ignored.map((reason) => <li key={reason}>{reason}</li>)}</ul>
          <p>The result is unaffected: hints only change how it is presented.</p>
        </details>}
      </div>

      {selected && <ViewBoundary view="detail">
        <InspectorPanel
          result={result}
          plan={plan}
          node={selected}
          onSelect={select}
          onClose={closeInspector}
          onShowInRaw={showInRaw}
        />
      </ViewBoundary>}
    </div>
  </div>;
}
