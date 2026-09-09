import { useId, useLayoutEffect, useRef, useState } from "react";
import type { GraphObject, Relationship } from "../pages/applicationModelTypes";

export function neighborhood(
  objects: GraphObject[],
  edges: Relationship[],
  selected: string,
  depth: number,
) {
  if (!selected) return objects.slice(0, 24);
  const ids = new Set([selected]);
  for (let i = 0; i < depth; i++) {
    const next = edges
      .filter((e) => ids.has(e.source) || ids.has(e.target))
      .flatMap((e) => [e.source, e.target]);
    next.slice(0, 200).forEach((id) => ids.add(id));
  }
  return objects.filter((o) => ids.has(o.id)).slice(0, 100);
}

type Box = { x: number; y: number; width: number; height: number };
export function graphLayout(
  objects: GraphObject[],
  sizes: Record<string, { width: number; height: number }>,
  width: number,
  minimumHeight = 400,
) {
  const columns = Math.max(
    1,
    Math.min(
      objects.length || 1,
      Math.floor(width / 280),
      Math.ceil(
        Math.sqrt((objects.length * width) / Math.max(400, minimumHeight)),
      ),
    ),
  );
  const cellWidth = (width - 40) / columns;
  const rowY = [40];
  for (let i = 0; i < objects.length; i += columns) {
    rowY.push(
      rowY[rowY.length - 1] +
        Math.max(
          64,
          ...objects
            .slice(i, i + columns)
            .map((o) => sizes[o.id]?.height ?? 64),
        ) +
        86,
    );
  }
  const rows = Math.ceil(objects.length / columns);
  const naturalHeight = Math.max(400, rowY[rows] ?? 400);
  const height = Math.max(naturalHeight, minimumHeight);
  const extra = height - naturalHeight;
  return {
    height,
    positions: new Map(
      objects.map((o, i) => {
        const size = sizes[o.id] ?? { width: 220, height: 64 };
        const row = Math.floor(i / columns);
        return [
          o.id,
          {
            ...size,
            x:
              20 +
              (i % columns) * cellWidth +
              Math.max(0, (cellWidth - size.width) / 2),
            y: rowY[row] + (rows > 1 ? (extra * row) / (rows - 1) : extra / 2),
          },
        ];
      }),
    ),
  };
}

export function connectionEndpoints(a: Box, b: Box) {
  const ax = a.x + a.width / 2,
    ay = a.y + a.height / 2;
  const bx = b.x + b.width / 2,
    by = b.y + b.height / 2;
  const dx = bx - ax,
    dy = by - ay;
  if (!dx && !dy)
    return { x1: a.x + a.width + 10, y1: ay, x2: ax, y2: a.y - 10 };
  const length = Math.hypot(dx, dy);
  const boundary = (box: Box) =>
    Math.min(
      dx ? box.width / 2 / Math.abs(dx) : Infinity,
      dy ? box.height / 2 / Math.abs(dy) : Infinity,
    ) +
    10 / length;
  const start = boundary(a),
    end = boundary(b);
  return {
    x1: ax + dx * start,
    y1: ay + dy * start,
    x2: bx - dx * end,
    y2: by - dy * end,
  };
}

export function ApplicationModelGraph({
  objects,
  layoutObjects,
  relationships,
  selected,
  depth,
  onSelect,
  onRelationship,
  expanded = false,
}: {
  objects: GraphObject[];
  layoutObjects?: GraphObject[];
  relationships: Relationship[];
  selected: string;
  depth: number;
  onSelect: (id: string) => void;
  onRelationship: (id: string) => void;
  expanded?: boolean;
}) {
  const canvas = useRef<HTMLDivElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const [canvasWidth, setCanvasWidth] = useState(840);
  const [canvasHeight, setCanvasHeight] = useState(400);
  const markerId = useId().replace(/:/g, "");
  const [sizes, setSizes] = useState<
    Record<string, { width: number; height: number }>
  >({});
  useLayoutEffect(() => {
    const nodes =
      canvas.current?.querySelectorAll<HTMLElement>("[data-node-id]");
    const measure = () => {
      if (scroll.current?.clientWidth)
        setCanvasWidth(Math.max(260, scroll.current.clientWidth));
      if (expanded && scroll.current && canvas.current) {
        const contentTop =
          canvas.current.getBoundingClientRect().top -
          scroll.current.getBoundingClientRect().top +
          scroll.current.scrollTop;
        setCanvasHeight(
          Math.max(0, Math.floor(scroll.current.clientHeight - contentTop)),
        );
      }
      setSizes((previous) => {
        const next = { ...previous };
        let changed = false;
        nodes?.forEach((node) => {
          const id = node.dataset.nodeId!;
          const { width, height } = node.getBoundingClientRect();
          if (
            width &&
            height &&
            (next[id]?.width !== width || next[id]?.height !== height)
          ) {
            next[id] = { width, height };
            changed = true;
          }
        });
        return changed ? next : previous;
      });
    };
    measure();
    const observer = new ResizeObserver(measure);
    nodes?.forEach((node) => observer.observe(node));
    if (scroll.current) observer.observe(scroll.current);
    return () => observer.disconnect();
  }, [objects, selected, depth, expanded]);
  const visible = neighborhood(objects, relationships, selected, depth);
  // Selected neighborhoods use local insertion order and fit the available width.
  const ordered = selected ? visible : (layoutObjects ?? objects);
  const { positions, height } = graphLayout(
    ordered,
    sizes,
    canvasWidth,
    expanded ? canvasHeight : 400,
  );
  const ids = new Set(visible.map((o) => o.id));
  const edges = relationships
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .slice(0, 100);
  return (
    <div
      ref={scroll}
      className="am-graph-scroll"
      tabIndex={0}
      aria-label="Relationship map; scroll to explore"
    >
      <p className="am-hint">
        Showing {visible.length} of {objects.length.toLocaleString()} objects ·{" "}
        {edges.length} connections
      </p>
      <div
        ref={canvas}
        className="am-graph-canvas"
        style={{ height, width: canvasWidth }}
      >
        <svg
          width={canvasWidth}
          height={height}
          aria-hidden="true"
          className="am-edges"
        >
          <defs>
            <marker
              id={markerId}
              markerWidth="8"
              markerHeight="8"
              refX="8"
              refY="4"
              orient="auto"
            >
              <path d="M0,0 L8,4 L0,8" fill="currentColor" />
            </marker>
          </defs>
          {edges.map((e) => {
            const a = positions.get(e.source)!,
              b = positions.get(e.target)!;
            if (e.source === e.target)
              return (
                <path
                  key={e.id}
                  d={`M ${a.x + a.width + 10} ${a.y + a.height / 2} C ${a.x + a.width + 60} ${a.y + a.height / 2}, ${a.x + a.width + 60} ${a.y - 25}, ${a.x + a.width / 2} ${a.y - 10}`}
                  fill="none"
                  className={`am-edge ${e.claim.status}`}
                  markerEnd={`url(#${markerId})`}
                />
              );
            return (
              <line
                key={e.id}
                {...connectionEndpoints(a, b)}
                data-source={e.source}
                data-target={e.target}
                className={`am-edge ${e.claim.status}`}
                markerEnd={`url(#${markerId})`}
              />
            );
          })}
        </svg>
        {edges.map((e, i) => {
          const a = positions.get(e.source)!,
            b = positions.get(e.target)!;
          return (
            <button
              key={e.id}
              className={`am-edge-label ${e.claim.status}`}
              style={{
                left: (a.x + b.x) / 2 + 5,
                top: (a.y + b.y) / 2 + 70 + (i % 2) * 20,
              }}
              onClick={() => onRelationship(e.id)}
              aria-label={`Inspect ${e.type} relationship`}
            >
              {e.type} → <small>{e.claim.status}</small>
            </button>
          );
        })}
        {visible.map((o) => (
          <button
            key={o.id}
            data-node-id={o.id}
            className={`am-node ${o.id === selected ? "selected" : ""}`}
            style={{
              left: positions.get(o.id)!.x,
              top: positions.get(o.id)!.y,
            }}
            onClick={() => onSelect(o.id)}
          >
            <strong>{o.label}</strong>
            <small>
              {String(o.classification.value)} · {o.classification.status}
            </small>
          </button>
        ))}
      </div>
      {!visible.length && <p>No objects match these filters.</p>}
    </div>
  );
}
