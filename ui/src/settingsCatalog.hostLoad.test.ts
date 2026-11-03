import { describe, expect, it } from "vitest";
import { settingCatalogEntry, settingCatalogText } from "./settingsCatalog";

describe("Butler host-load discovery", () => {
  it("includes direct read operations and freshness terms in the existing setup destination", () => {
    const setup = settingCatalogEntry("settings.setup");
    const text = settingCatalogText(setup).toLowerCase();
    for (const term of ["cpu", "memory", "fresh host load", "sample age", "nebula.work.host_load", "nebula.global.host_load"]) {
      expect(text).toContain(term);
    }
    expect(setup.target).toBe("setup-settings");
  });
});
