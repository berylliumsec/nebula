import { useEffect, useState } from "react";
import type { GraphObject } from "../pages/applicationModelTypes";

export function ApplicationModelCategory({
  name,
  objects,
  selected,
  query,
  onSelect,
}: {
  name: string;
  objects: GraphObject[];
  selected: string;
  query: string;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(0);
  const selectedIndex = objects.findIndex((o) => o.id === selected);
  useEffect(() => {
    if (selectedIndex >= 0 || query) setOpen(true);
    setPage(selectedIndex >= 0 ? Math.floor(selectedIndex / 20) : 0);
  }, [selected, query, selectedIndex]);
  const current = Math.min(
    page,
    Math.max(0, Math.ceil(objects.length / 20) - 1),
  );
  return (
    <details
      className="am-category"
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>
        {name}
        <span>{objects.length.toLocaleString()}</span>
      </summary>
      {open && (
        <>
          {objects.slice(current * 20, current * 20 + 20).map((o) => (
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
          {objects.length > 20 && (
            <nav className="am-pagination" aria-label={`${name} object pages`}>
              <button
                disabled={!current}
                onClick={() => setPage(current - 1)}
                aria-label={`Previous ${name} objects`}
              >
                ←
              </button>
              <span>
                {current * 20 + 1}–{Math.min(objects.length, current * 20 + 20)}{" "}
                of {objects.length.toLocaleString()}
              </span>
              <button
                disabled={(current + 1) * 20 >= objects.length}
                onClick={() => setPage(current + 1)}
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
