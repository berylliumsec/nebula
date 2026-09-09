import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  ApplicationModelGraph,
  neighborhood,
  connectionEndpoints,
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
