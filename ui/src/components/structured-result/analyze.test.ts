import { describe, expect, it } from "vitest";
import { analyze, SAMPLE_LIMIT } from "./analyze";
import { NormalizedResult } from "./normalize";

const analysisOf = (value: unknown) => analyze(new NormalizedResult(value));

describe("classifying a result by its shape alone", () => {
  it("treats a flat object as a record and leads with familiar field names", () => {
    const analysis = analysisOf({ port: 443, status: "unknown-state", name: "edge", extra: true });
    expect(analysis.shape).toBe("record");
    // Presentation order only: `name` and `status` are shown first because
    // operators look there, not because they were interpreted.
    expect(analysis.overviewFields.map((field) => field.key)).toEqual(["name", "status", "port", "extra"]);
    expect(analysis.tables).toHaveLength(0);
  });

  it("offers a table for a homogeneous array of objects and unions their keys", () => {
    const analysis = analysisOf({ hosts: [{ ip: "10.0.0.1", up: true }, { ip: "10.0.0.2", note: "seen" }] });
    expect(analysis.tables).toHaveLength(1);
    expect(analysis.tables[0].columns).toEqual(["ip", "up", "note"]);
    expect(analysis.tables[0].rowCount).toBe(2);
    expect(analysis.tables[0].sampled).toBe(false);
  });

  it("makes the root itself a table when the result is the collection", () => {
    const analysis = analysisOf([{ id: 1 }, { id: 2 }]);
    expect(analysis.shape).toBe("collection");
    expect(analysis.tables[0].path).toBe("$");
  });

  it("leaves scalar arrays and heterogeneous arrays to the tree", () => {
    expect(analysisOf({ ports: [80, 443] }).tables).toHaveLength(0);
    expect(analysisOf({ mixed: [1, "two", { three: 3 }] }).tables).toHaveLength(0);
    // A mostly-object array is still a table; one stray entry does not lose it.
    const mostly = analysisOf({ rows: [{ a: 1 }, { a: 2 }, { a: 3 }, "stray"] });
    expect(mostly.tables).toHaveLength(1);
  });

  it("classifies a large collection from a bounded sample and says so", () => {
    const rows = Array.from({ length: 5_000 }, (_item, index) => ({ index, name: `row-${index}` }));
    const analysis = analysisOf({ rows });
    expect(analysis.tables[0].rowCount).toBe(5_000);
    expect(analysis.tables[0].sampled).toBe(true);
    expect(analysis.sampled).toBe(true);
    // Sampling bounds classification, never access: the row count is exact.
    expect(SAMPLE_LIMIT).toBeLessThan(5_000);
  });

  it("recognises nodes and edges, and the endpoint spellings it was told about", () => {
    for (const [source, target] of [["source", "target"], ["src", "dst"], ["from", "to"], ["source_id", "target_id"]]) {
      const analysis = analysisOf({
        nodes: [{ id: "a" }, { id: "b" }],
        edges: [{ [source]: "a", [target]: "b" }],
      });
      expect(analysis.graphs).toHaveLength(1);
      expect(analysis.graphs[0].sourceKey).toBe(source);
      expect(analysis.graphs[0].targetKey).toBe(target);
      expect(analysis.graphs[0].derivedNodes).toBe(false);
    }
  });

  it("derives nodes from endpoints when a result publishes relationships alone", () => {
    const analysis = analysisOf({ links: [{ from: "a", to: "b" }, { from: "b", to: "c" }] });
    expect(analysis.graphs).toHaveLength(1);
    expect(analysis.graphs[0].derivedNodes).toBe(true);
    expect(analysis.graphs[0].edgeCount).toBe(2);
  });

  it("refuses to invent a graph from something that only resembles one", () => {
    // Nodes without relationships.
    expect(analysisOf({ nodes: [{ id: "a" }] }).graphs).toHaveLength(0);
    // Half an endpoint pair.
    expect(analysisOf({ nodes: [{ id: "a" }], edges: [{ from: "a" }] }).graphs).toHaveLength(0);
    // Endpoint names that were never part of the recognised vocabulary.
    expect(analysisOf({ edges: [{ start: "a", end: "b" }] }).graphs).toHaveLength(0);
    // An empty relationship collection.
    expect(analysisOf({ nodes: [{ id: "a" }], edges: [] }).graphs).toHaveLength(0);
  });

  it("reports endpoints that name no published node without dropping them", () => {
    const analysis = analysisOf({
      nodes: [{ id: "a" }],
      edges: [{ source: "a", target: "ghost" }],
    });
    expect(analysis.graphs[0].danglingEndpoints).toBe(1);
    expect(analysis.graphs[0].nodeCount).toBe(1);
  });

  it("finds tables nested inside a result, and a table that is also a graph only once", () => {
    const analysis = analysisOf({ scan: { findings: [{ id: 1 }, { id: 2 }] } });
    expect(analysis.tables.map((table) => table.path)).toEqual(["$.scan.findings"]);

    const graphed = analysisOf({ edges: [{ source: "a", target: "b" }, { source: "b", target: "c" }] });
    expect(graphed.graphs).toHaveLength(1);
    // The same collection is still available as a table; it is not consumed.
    expect(graphed.tables.map((table) => table.path)).toEqual(["$.edges"]);
  });

  it("describes empty and null-heavy results without failing", () => {
    expect(analysisOf({}).shape).toBe("empty");
    expect(analysisOf([]).shape).toBe("empty");
    expect(analysisOf(null).shape).toBe("scalar");
    const nulls = analysisOf({ a: null, b: null, c: [null, null] });
    expect(nulls.overviewFields.map((field) => field.key)).toEqual(["a", "b"]);
    expect(nulls.collections.map((child) => child.key)).toEqual(["c"]);
    expect(nulls.tables).toHaveLength(0);
  });

  it("survives a deeply nested document without recursing through it", () => {
    let deep: unknown = "leaf";
    for (let index = 0; index < 400; index += 1) deep = { next: deep };
    const analysis = analysisOf(deep);
    expect(analysis.shape).toBe("record");
    expect(analysis.collections).toHaveLength(1);
  });
});
