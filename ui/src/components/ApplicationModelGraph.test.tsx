import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  ApplicationModelGraph,
  neighborhood,
  connectionEndpoints,
  graphLayout,
} from "./ApplicationModelGraph";
import { blankClaim, type GraphObject } from "../pages/applicationModelTypes";
const objects: GraphObject[] = ["Page", "Form", "Input"].map((type, i) => ({
  id: String(i),
  label: type,
  classification: blankClaim(type),
  authentication_context: "anonymous",
  properties: {},
  revision: 1,
}));
const relationships = [
  {
    id: "edge",
    type: "contains",
    source: "0",
    target: "1",
    claim: blankClaim(true),
  },
  {
    id: "input",
    type: "accepts_input",
    source: "1",
    target: "2",
    claim: blankClaim(true),
  },
];
const wideObjects = Array.from({ length: 11 }, (_, i) => ({
  ...objects[0],
  id: `wide-${i}`,
}));
it("uses the full width of a wide fullscreen graph rather than three fixed columns", () => {
  const { positions } = graphLayout(wideObjects, {}, 2800, 1000);
  const boxes = [...positions.values()];
  expect(new Set(boxes.map((b) => b.x)).size).toBeGreaterThan(3);
  expect(
    Math.max(...boxes.map((b) => b.x + b.width)) -
      Math.min(...boxes.map((b) => b.x)),
  ).toBeGreaterThan(2500);
  expect(Math.max(...boxes.map((b) => b.y + b.height))).toBeGreaterThan(800);
});
it("reflows between narrow and wide containers without resizing or clipping cards", () => {
  for (const width of [260, 320, 840, 1440, 2800]) {
    const { positions, height } = graphLayout(wideObjects, {}, width);
    for (const b of positions.values()) {
      expect(b.x).toBeGreaterThanOrEqual(0);
      expect(b.x + b.width).toBeLessThanOrEqual(width);
      expect(b.y + b.height).toBeLessThan(height);
      expect(b.width).toBe(220);
    }
  }
});
it("retains room for long cards and releases fullscreen height on restore", () => {
  const sizes = { "wide-0": { width: 220, height: 240 } };
  const expanded = graphLayout(wideObjects, sizes, 1440, 1200);
  expect(expanded.height).toBe(1200);
  expect(expanded.positions.get("wide-5")!.y).toBeGreaterThan(
    expanded.positions.get("wide-0")!.y + 240,
  );
  expect(graphLayout(wideObjects, sizes, 1440).height).toBeLessThan(
    expanded.height,
  );
});
describe("project graph", () => {
  it("keeps object positions when the outline is filtered", () => {
    const props = {
      relationships,
      selected: "",
      depth: 1,
      onSelect: vi.fn(),
      onRelationship: vi.fn(),
      layoutObjects: objects,
    };
    const { rerender } = render(
      <ApplicationModelGraph {...props} objects={objects} />,
    );
    const position = screen
      .getByRole("button", { name: /Form\s*Form/ })
      .getAttribute("style");
    rerender(<ApplicationModelGraph {...props} objects={[objects[1]]} />);
    expect(
      screen.getByRole("button", { name: /Form\s*Form/ }).getAttribute("style"),
    ).toBe(position);
  });

  it("expands one neighborhood at a time and preserves node order", () => {
    expect(
      neighborhood(objects, relationships, "0", 1).map((o) => o.id),
    ).toEqual(["0", "1"]);
    expect(
      neighborhood(objects, relationships, "0", 2).map((o) => o.id),
    ).toEqual(["0", "1", "2"]);
  });
  it("labels directional relationships and exposes inspection by keyboard-accessible buttons", () => {
    const select = vi.fn();
    render(
      <ApplicationModelGraph
        objects={objects}
        relationships={relationships}
        selected="0"
        depth={1}
        onSelect={vi.fn()}
        onRelationship={select}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Inspect contains relationship" }),
    );
    expect(select).toHaveBeenCalledWith("edge");
    expect(
      screen.queryByRole("button", {
        name: "Inspect accepts_input relationship",
      }),
    ).not.toBeInTheDocument();
  });
});

it.each([
  [280, 0, 64],
  [-280, 0, 64],
  [0, 250, 64],
  [0, -250, 64],
  [280, 250, 180],
])(
  "keeps arrow endpoints outside cards at %s,%s with height %s",
  (x, y, height) => {
    const a = { x: 0, y: 0, width: 220, height: 64 },
      b = { x, y, width: 220, height };
    const edge = connectionEndpoints(a, b);
    const inside = (px: number, py: number, box: typeof a) =>
      px >= box.x &&
      px <= box.x + box.width &&
      py >= box.y &&
      py <= box.y + box.height;
    expect(inside(edge.x1, edge.y1, a)).toBe(false);
    expect(inside(edge.x2, edge.y2, b)).toBe(false);
    expect(Object.values(edge).every(Number.isFinite)).toBe(true);
  },
);
