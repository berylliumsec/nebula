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

export function ApplicationModelGraph({
  objects,
  layoutObjects,
  relationships,
  selected,
  depth,
  onSelect,
  onRelationship,
}: {
  objects: GraphObject[];
  layoutObjects?: GraphObject[];
  relationships: Relationship[];
  selected: string;
  depth: number;
  onSelect: (id: string) => void;
  onRelationship: (id: string) => void;
}) {
  const visible = neighborhood(objects, relationships, selected, depth);
  // Durable insertion order owns positions. Selection/expansion never reorders nodes.
  const positions = new Map(
    (layoutObjects ?? objects).map((o, i) => [
      o.id,
      { x: (i % 3) * 280 + 20, y: Math.floor(i / 3) * 150 + 40 },
    ]),
  );
  const ids = new Set(visible.map((o) => o.id));
  const edges = relationships
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .slice(0, 100);
  const height = Math.max(
    400,
    ...visible.map((o) => (positions.get(o.id)?.y ?? 0) + 130),
  );
  return (
    <div
      className="am-graph-scroll"
      tabIndex={0}
      aria-label="Relationship map; scroll to explore"
    >
      <div className="am-graph-canvas" style={{ height, width: 840 }}>
        <svg
          width={840}
          height={height}
          aria-hidden="true"
          className="am-edges"
        >
          <defs>
            <marker
              id="am-arrow"
              markerWidth="8"
              markerHeight="8"
              refX="7"
              refY="4"
              orient="auto"
            >
              <path d="M0,0 L8,4 L0,8" fill="currentColor" />
            </marker>
          </defs>
          {edges.map((e) => {
            const a = positions.get(e.source)!,
              b = positions.get(e.target)!;
            return (
              <line
                key={e.id}
                x1={a.x + 110}
                y1={a.y + 50}
                x2={b.x + 110}
                y2={b.y + 10}
                className={`am-edge ${e.claim.status}`}
                markerEnd="url(#am-arrow)"
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
