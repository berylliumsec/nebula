import { describe, expect, it } from "vitest";
import { nebulaHighlightStyle } from "./editorTheme";

describe("nebula highlight style", () => {
  it("resolves every colour through a theme token instead of a literal", () => {
    const rules = nebulaHighlightStyle.module?.getRules() ?? "";
    expect(rules).toContain("var(--code-keyword)");
    expect(rules).toContain("var(--code-string)");
    expect(rules).toContain("var(--code-comment)");
    // A literal would freeze one theme's palette into every other theme.
    expect(rules).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(rules).not.toMatch(/\brgba?\(/i);
  });
});
