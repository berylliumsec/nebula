import { describe, expect, it } from "vitest";
import { analyze } from "./analyze";
import { buildPlan, readHints } from "./hints";
import { NormalizedResult } from "./normalize";

const planFor = (value: unknown, hints: unknown) => {
  const result = new NormalizedResult(value);
  return buildPlan(result, analyze(result), hints);
};

const record = {
  name: "edge-01",
  secret: "hunter2",
  status: "unknown-state",
  notes: "long form",
  rows: [{ a: 1, b: 2, c: 3 }],
};

describe("optional presentation hints", () => {
  it("renders the result unchanged when there are no hints at all", () => {
    for (const hints of [undefined, null, {}]) {
      const plan = planFor(record, hints);
      expect(plan.ignored).toEqual([]);
      expect(plan.analysis.overviewFields.map((field) => field.key)).toEqual(["name", "status", "secret", "notes"]);
      expect(plan.analysis.tables[0].columns).toEqual(["a", "b", "c"]);
    }
  });

  it("orders, labels and hides fields a producer asked about", () => {
    const plan = planFor(record, {
      titleField: "notes",
      fieldOrder: ["status", "name"],
      hiddenFields: ["secret"],
      labels: { name: "Host name" },
      descriptions: { status: "As the scanner reported it" },
    });
    expect(plan.analysis.overviewFields.map((field) => field.key)).toEqual(["notes", "status", "name"]);
    // Hidden means hidden by default: the field is still handed to the interface.
    expect(plan.hiddenOverviewFields.map((field) => field.key)).toEqual(["secret"]);
    expect(plan.labels.name).toBe("Host name");
    expect(plan.descriptions.status).toContain("scanner");
  });

  it("keeps unlisted table columns available instead of dropping them", () => {
    const plan = planFor(record, { tableColumns: { "$.rows": ["c"] } });
    expect(plan.analysis.tables[0].columns).toEqual(["c", "a", "b"]);
    expect(plan.hiddenColumns["$.rows"]).toEqual(["a", "b"]);
  });

  it("marks redaction without removing the value", () => {
    const plan = planFor(record, { redactFields: ["secret"] });
    expect(plan.redacted).toEqual(["secret"]);
    expect(plan.analysis.overviewFields.some((field) => field.key === "secret")).toBe(true);
  });

  it("maps a graph the analyzer would not have recognised", () => {
    const plan = planFor(
      { people: [{ key: "a" }], ties: [{ parent: "a", child: "b" }] },
      { graph: { nodesPath: "$.people", edgesPath: "$.ties", sourceKey: "parent", targetKey: "child", nodeIdKey: "key" } },
    );
    expect(plan.analysis.graphs).toHaveLength(1);
    expect(plan.analysis.graphs[0]).toMatchObject({ sourceKey: "parent", targetKey: "child", nodeIdKey: "key" });
    expect(plan.ignored).toEqual([]);
  });

  it("ignores each invalid hint on its own and still renders the result", () => {
    const plan = planFor(record, {
      titleField: 42,
      fieldOrder: "status",
      hiddenFields: ["secret", 7],
      labels: "not a map",
      formats: { name: "hologram" },
      tableColumns: { "$.rows": ["absent-column"] },
      graph: { edgesPath: "$.nowhere" },
      unknownHint: true,
    });
    expect(plan.ignored.length).toBeGreaterThanOrEqual(5);
    expect(plan.ignored.join(" ")).toContain("titleField");
    expect(plan.ignored.join(" ")).toContain("hologram");
    expect(plan.ignored.join(" ")).toContain("$.nowhere");
    // The valid part of a partly-invalid document still applies.
    expect(plan.hiddenOverviewFields.map((field) => field.key)).toEqual(["secret"]);
    // And the result itself is untouched.
    expect(plan.analysis.tables[0].columns).toEqual(["a", "b", "c"]);
    expect(plan.analysis.root.value).toBe(record);
  });

  it("ignores a hints document that is not an object", () => {
    expect(readHints("nonsense").ignored[0]).toContain("not an object");
    expect(readHints(["a"]).hints).toEqual({});
    expect(readHints(undefined)).toEqual({ hints: {}, ignored: [] });
  });
});
