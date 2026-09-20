import { describe, expect, it } from "vitest";
import { renderRaw } from "./rawText";

describe("printing the raw result", () => {
  it("prints exactly what JSON.stringify would, for every shape", () => {
    for (const value of [
      { a: 1, b: [1, 2, { c: null }], d: "text" },
      [1, [2, [3]], {}],
      { empty: {}, none: [] },
      "bare string",
      42,
      true,
      null,
      { "odd key": "value", "quote\"d": "x", unicode: "é\n\t" },
      { big: 9007199254740993, tiny: 1e-7, negative: -0.5 },
    ]) {
      expect(renderRaw(value).lines.join("\n")).toBe(JSON.stringify(value, null, 2));
    }
  });

  it("ties a value's path to the line it starts on", () => {
    const document_ = renderRaw({ outer: { inner: [10, 20] }, tail: "last" });
    const lines = document_.lines;
    expect(lines[document_.lineForPath.get("$.outer")!]).toContain('"outer"');
    expect(lines[document_.lineForPath.get("$.outer.inner[1]")!].trim()).toBe("20");
    expect(lines[document_.lineForPath.get("$.tail")!]).toContain('"last"');
  });

  it("prints a self-referential document instead of looping forever", () => {
    const loop: Record<string, unknown> = { name: "loop" };
    loop.self = loop;
    const text = renderRaw(loop).lines.join("\n");
    expect(text).toContain("[circular reference]");
    expect(text).toContain('"name": "loop"');
  });

  it("stops after its line budget and says the text was cut", () => {
    const wide = Array.from({ length: 300_000 }, (_item, index) => index);
    const document_ = renderRaw(wide);
    expect(document_.truncated).toBe(true);
    expect(document_.lines.length).toBeLessThanOrEqual(200_000);
  });
});
