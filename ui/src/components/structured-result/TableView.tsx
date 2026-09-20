import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, Columns3, PanelRight } from "lucide-react";
import { IconAction } from "../IconAction";
import type { TableCandidate } from "./analyze";
import { compactValue, copyText, formatValue } from "./format";
import type { PresentationPlan } from "./hints";
import { isScalar, type NormalizedResult, type ResultNode } from "./normalize";
import { fieldLabel, isRedacted, type NodeSelection } from "./PropertyGrid";
import { CopyAction, ScalarValue } from "./ValueView";

const PAGE_SIZES = [25, 50, 100];
/** Rows a filter or a sort may reorder before the table says so out loud. */
const ORDERING_LIMIT = 20_000;

type Direction = "ascending" | "descending";

function cellValue(row: ResultNode, column: string): unknown {
  return row.type === "object" ? (row.value as Record<string, unknown>)[column] : undefined;
}

function compare(left: unknown, right: unknown): number {
  if (left === right) return 0;
  // Absent and null values sort last in either direction: their absence is
  // reported, never treated as a low value.
  if (left === undefined || left === null) return 1;
  if (right === undefined || right === null) return -1;
  if (typeof left === "number" && typeof right === "number") return left - right;
  if (typeof left === "boolean" && typeof right === "boolean") return Number(left) - Number(right);
  return String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: "base" });
}

interface TableViewProps extends NodeSelection {
  result: NormalizedResult;
  plan: PresentationPlan;
  table: TableCandidate;
}

/**
 * A homogeneous array of objects, shown as a table inferred from the union of
 * the sampled rows' keys. Columns a row does not carry are marked absent
 * rather than blank, and a row with keys outside the inferred columns keeps
 * them one click away in the inspector.
 */
export function TableView({ result, plan, table, selected, onSelect }: TableViewProps) {
  const [sort, setSort] = useState<{ column: string; direction: Direction }>();
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);
  const [hidden, setHidden] = useState<string[]>(() => plan.hiddenColumns[table.path] ?? []);
  const [columnsOpen, setColumnsOpen] = useState(false);

  const columns = table.columns.filter((column) => !hidden.includes(column));
  const needle = filter.trim().toLowerCase();
  const ordering = Boolean(sort) || needle.length > 0;

  const rows = useMemo(() => {
    const window = result.childWindow(table.node, 0, ordering ? ORDERING_LIMIT : Number.POSITIVE_INFINITY);
    const filtered = needle
      ? window.filter((row) => copyText(row.value).toLowerCase().includes(needle))
      : window;
    if (!sort) return filtered;
    const direction = sort.direction === "ascending" ? 1 : -1;
    return [...filtered].sort((left, right) => direction * compare(cellValue(left, sort.column), cellValue(right, sort.column)));
  }, [needle, ordering, result, sort, table.node]);

  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const current = Math.min(page, pageCount - 1);
  const visible = rows.slice(current * pageSize, current * pageSize + pageSize);

  const toggleSort = (column: string) => {
    setPage(0);
    setSort((value) =>
      value?.column === column
        ? value.direction === "ascending" ? { column, direction: "descending" } : undefined
        : { column, direction: "ascending" },
    );
  };

  return <div className="structured-table">
    <div className="structured-table-controls">
      <label className="structured-search">
        <span className="sr-only">Filter {table.label} rows</span>
        <input type="search" value={filter} placeholder={`Filter ${table.rowCount.toLocaleString()} rows`} onChange={(event) => { setFilter(event.target.value); setPage(0); }} />
      </label>
      <div className="structured-table-columns">
        <button type="button" className="button quiet" aria-expanded={columnsOpen} onClick={() => setColumnsOpen(!columnsOpen)}>
          <Columns3 size={15} aria-hidden="true" /> Columns ({columns.length}/{table.columns.length})
        </button>
        {columnsOpen && <fieldset className="structured-column-menu">
          <legend>Columns shown</legend>
          {table.columns.map((column) => <label key={column}>
            <input
              type="checkbox"
              checked={!hidden.includes(column)}
              onChange={() => setHidden(hidden.includes(column) ? hidden.filter((item) => item !== column) : [...hidden, column])}
            />
            {plan.labels[column] ?? column}
          </label>)}
        </fieldset>}
      </div>
    </div>

    {table.sampled && <p className="structured-derived">
      Columns were inferred from the first {Math.min(table.rowCount, 200).toLocaleString()} of {table.rowCount.toLocaleString()} rows. A row carrying other fields still shows them in its detail.
    </p>}
    {ordering && table.rowCount > ORDERING_LIMIT && <p className="structured-derived" role="status">
      Filtering and sorting cover the first {ORDERING_LIMIT.toLocaleString()} rows. Clear both to page through all {table.rowCount.toLocaleString()}.
    </p>}

    <div className="structured-table-scroll">
      <table>
        <caption className="sr-only">{table.label}: {table.rowCount.toLocaleString()} rows</caption>
        <thead>
          <tr>
            <th scope="col" className="structured-table-index">#</th>
            {columns.map((column) => {
              const active = sort?.column === column;
              return <th key={column} scope="col" aria-sort={active ? sort!.direction : "none"}>
                <button type="button" onClick={() => toggleSort(column)}>
                  {plan.labels[column] ?? column}
                  {active
                    ? sort!.direction === "ascending" ? <ArrowUp size={13} aria-hidden="true" /> : <ArrowDown size={13} aria-hidden="true" />
                    : null}
                  <span className="sr-only">{active ? `sorted ${sort!.direction}` : "not sorted"}</span>
                </button>
              </th>;
            })}
            <th scope="col"><span className="sr-only">Row detail</span></th>
          </tr>
        </thead>
        <tbody>
          {visible.map((row) => {
            const extras = row.type === "object" ? Object.keys(row.value as object).filter((key) => !table.columns.includes(key)) : [];
            return <tr key={row.path} data-selected={selected?.path === row.path ? "true" : undefined}>
              <th scope="row" className="structured-table-index">{typeof row.key === "number" ? row.key : ""}</th>
              {columns.map((column) => {
                const value = cellValue(row, column);
                const present = row.type === "object" && column in (row.value as object);
                const child = present ? result.childAt(row, column) : undefined;
                const numeric = formatValue(column, value).alignEnd;
                return <td key={column} className={numeric ? "structured-cell-number" : undefined}>
                  {!present
                    ? <span className="structured-absent" title={`${column} is not present in this row`}>not present</span>
                    : child && !isScalar(child.type)
                      ? <button type="button" className="structured-nested" onClick={(event) => onSelect(child, event.currentTarget)}>{compactValue(child.value)}</button>
                      : <ScalarValue name={column} value={value} format={plan.formats[column]} redacted={child ? isRedacted(plan, child) : false} />}
                </td>;
              })}
              <td className="structured-table-actions">
                {extras.length > 0 && <button type="button" className="structured-nested" onClick={(event) => onSelect(row, event.currentTarget)}>
                  +{extras.length} more {extras.length === 1 ? "field" : "fields"}
                </button>}
                <CopyAction text={copyText(row.value)} label={`Copy row ${row.key}`} />
                <IconAction icon={PanelRight} label={`Inspect row ${row.key}`} onClick={(event) => onSelect(row, event.currentTarget)} />
              </td>
            </tr>;
          })}
          {visible.length === 0 && <tr>
            <td colSpan={columns.length + 2}>
              <p className="structured-empty">No row matches “{filter}”. Every row is still in the tree and raw views.</p>
            </td>
          </tr>}
        </tbody>
      </table>
    </div>

    <div className="structured-table-footer">
      <p role="status">
        {rows.length === 0 ? "No rows" : `Rows ${current * pageSize + 1}–${Math.min(rows.length, (current + 1) * pageSize)} of ${rows.length.toLocaleString()}`}
        {needle && table.rowCount !== rows.length && ` (filtered from ${table.rowCount.toLocaleString()})`}
      </p>
      <div className="structured-pager">
        <label>
          <span className="sr-only">Rows per page</span>
          <select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(0); }}>
            {PAGE_SIZES.map((size) => <option key={size} value={size}>{size} per page</option>)}
          </select>
        </label>
        <button type="button" className="button quiet" disabled={current === 0} onClick={() => setPage(current - 1)}>Previous</button>
        <span>Page {current + 1} of {pageCount}</span>
        <button type="button" className="button quiet" disabled={current + 1 >= pageCount} onClick={() => setPage(current + 1)}>Next</button>
      </div>
    </div>
  </div>;
}

/** Label used by the dashboard's table picker. */
export function tableLabel(plan: PresentationPlan, table: TableCandidate): string {
  const node = table.node;
  return node.key === undefined ? table.label : fieldLabel(plan, node);
}
