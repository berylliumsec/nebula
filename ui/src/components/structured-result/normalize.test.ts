import { describe, expect, it } from "vitest";
import {
  formatPath,
  isScalar,
  NormalizedResult,
  pathSegments,
  runtimeTypeOf,
  typeLabel,
} from "./normalize";

describe("normalizing an arbitrary JSON value", () => {
  it("describes every runtime type a JSON document can hold", () => {
    expect(runtimeTypeOf({})).toBe("object");
    expect(runtimeTypeOf([])).toBe("array");
    expect(runtimeTypeOf("text")).toBe("string");
    expect(runtimeTypeOf(0)).toBe("number");
    expect(runtimeTypeOf(false)).toBe("boolean");
    expect(runtimeTypeOf(null)).toBe("null");
    expect([true, false].map(isScalar as never)).toBeDefined();
    expect(isScalar("string")).toBe(true);
    expect(isScalar("object")).toBe(false);
  });

  it("keeps the original value on every node without copying it", () => {
    const rows = [{ name: "alpha" }, { name: "beta" }];
    const result = new NormalizedResult({ rows, note: null });
    const rowsNode = result.childAt(result.root, "rows");
    expect(rowsNode.value).toBe(rows);
    expect(result.childAt(rowsNode, 0).value).toBe(rows[0]);
    expect(result.childAt(result.root, "note").type).toBe("null");
  });

  it("builds paths that survive a round trip through their text form", () => {
    const result = new NormalizedResult({ "odd key": [{ "a.b": 1 }] });
    const odd = result.childAt(result.root, "odd key");
    const nested = result.childAt(result.childAt(odd, 0), "a.b");
    expect(odd.path).toBe('$["odd key"]');
    expect(nested.path).toBe('$["odd key"][0]["a.b"]');
    expect(result.resolve(pathSegments(nested.path))?.value).toBe(1);
    expect(formatPath(["a", 0, "b"])).toBe("$.a[0].b");
  });

  it("materialises only the window a caller asks for", () => {
    const result = new NormalizedResult(Array.from({ length: 50_000 }, (_item, index) => index));
    const window = result.childWindow(result.root, 10, 5);
    expect(window.map((node) => node.value)).toEqual([10, 11, 12, 13, 14]);
    expect(result.nodeAt("$[10]")).toBeDefined();
    expect(result.nodeAt("$[4000]")).toBeUndefined();
  });

  it("reports sizes an operator can read", () => {
    const result = new NormalizedResult({ one: [1], two: {}, three: "abc" });
    expect(typeLabel(result.root)).toBe("object · 3 properties");
    expect(typeLabel(result.childAt(result.root, "one"))).toBe("array · 1 item");
    expect(typeLabel(result.childAt(result.root, "two"))).toBe("object · 0 properties");
    expect(typeLabel(result.childAt(result.root, "three"))).toBe("string · 3 characters");
  });

  it("stops at a value that refers back into its own ancestors", () => {
    const loop: Record<string, unknown> = { name: "loop" };
    loop.self = loop;
    const result = new NormalizedResult(loop);
    const self = result.childAt(result.root, "self");
    expect(self.type).toBe("unsupported");
    expect(self.note).toContain("refers back");
    expect(result.hasChildren(self)).toBe(false);
  });

  it("names values JSON cannot carry instead of pretending they are objects", () => {
    const result = new NormalizedResult({ when: new Date(0), missing: undefined, fn: () => 1 });
    expect(result.childAt(result.root, "when").note).toContain("Date");
    expect(result.childAt(result.root, "missing").note).toContain("undefined");
    expect(result.childAt(result.root, "fn").note).toContain("function");
  });

  it("treats a scalar, an empty object and an empty array as valid roots", () => {
    expect(new NormalizedResult("just text").root.type).toBe("string");
    expect(new NormalizedResult(null).root.type).toBe("null");
    expect(new NormalizedResult({}).root.size).toBe(0);
    expect(new NormalizedResult([]).root.size).toBe(0);
  });
});
