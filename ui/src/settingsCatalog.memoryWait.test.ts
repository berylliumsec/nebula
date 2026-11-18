import { describe, expect, it } from "vitest";
import { settingCatalogEntry, settingCatalogText } from "./settingsCatalog";

describe("Waiting for memory discovery", () => {
  it("leads an operator whose project waits for memory to the Core reserve", () => {
    const budgets = settingCatalogEntry("settings.memory-budgets");
    const text = settingCatalogText(budgets).toLowerCase();
    for (const term of ["waiting for memory", "core reserve", "project workers", "more projects run at once"]) {
      expect(text).toContain(term);
    }
    expect(budgets.target).toBe("memory-budget-settings");
  });
});
