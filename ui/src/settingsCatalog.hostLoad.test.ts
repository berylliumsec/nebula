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
  it("lets an operator whose chats wait for host pressure find how to turn a check off", () => {
    const pressure = settingCatalogEntry("settings.host-pressure");
    const text = settingCatalogText(pressure).toLowerCase();
    for (const term of ["waiting for host pressure", "100% turns a check off", "turn off", "never starts"]) {
      expect(text).toContain(term);
    }
    expect(pressure.target).toBe("setup-settings");
  });
});
