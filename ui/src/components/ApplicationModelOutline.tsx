import { useEffect, useState } from "react";
import type { GraphObject } from "../pages/applicationModelTypes";

export function ApplicationModelCategory({
  name,
  objects,
  selected,
  query,
  onSelect,
  total,
  offset,
  expanded,
  onPage,
  onOpen,
}: {
  name: string;
  objects: GraphObject[];
  selected: string;
  query: string;
  onSelect: (id: string) => void;
  total?: number;
  offset?: number;
  expanded?: boolean;
  onPage?: (offset: number) => void;
  onOpen?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(0);
  const selectedIndex = objects.findIndex((o) => o.id === selected);
  useEffect(() => {
    if (selectedIndex >= 0 || query) setOpen(true);
    setPage(selectedIndex >= 0 ? Math.floor(selectedIndex / 20) : 0);
  }, [selected, query, selectedIndex]);
  useEffect(() => {
    if (expanded !== undefined) setOpen(expanded);
  }, [expanded]);
  const count = total ?? objects.length;
  const current = onPage
    ? Math.floor((offset ?? 0) / 20)
    : Math.min(page, Math.max(0, Math.ceil(objects.length / 20) - 1));
  return (
    <details
      className="am-category"
      open={open}
      onToggle={(e) => {
        setOpen(e.currentTarget.open);
        if (e.currentTarget.open && !expanded) onOpen?.();
      }}
    >
      <summary>
        {name}
        <span>{count.toLocaleString()}</span>
      </summary>
      {open && (
        <>
          {(onPage
            ? objects
            : objects.slice(current * 20, current * 20 + 20)
          ).map((o) => (
            <button
              key={o.id}
              className={`am-object ${o.id === selected ? "selected" : ""}`}
              aria-current={o.id === selected ? "true" : undefined}
              aria-label={`${o.label} · ${String(o.classification.value)} · ${o.classification.status}`}
              title={o.label}
              onClick={() => onSelect(o.id)}
            >
              <strong>{o.label}</strong>
              <small>
                {String(o.classification.value)} · {o.classification.status}
              </small>
            </button>
          ))}
          {count > 20 && (
            <nav className="am-pagination" aria-label={`${name} object pages`}>
              <button
                disabled={!current}
                onClick={() =>
                  onPage ? onPage((current - 1) * 20) : setPage(current - 1)
                }
                aria-label={`Previous ${name} objects`}
              >
                ←
              </button>
              <span>
                {current * 20 + 1}–{Math.min(count, current * 20 + 20)} of{" "}
                {count.toLocaleString()}
              </span>
              <button
                disabled={(current + 1) * 20 >= count}
                onClick={() =>
                  onPage ? onPage((current + 1) * 20) : setPage(current + 1)
                }
                aria-label={`Next ${name} objects`}
              >
                →
              </button>
            </nav>
          )}
        </>
      )}
    </details>
  );
}
