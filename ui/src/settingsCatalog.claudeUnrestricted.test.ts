import { describe, expect, it } from "vitest";
import { settingCatalog, settingCatalogEntry, settingCatalogText } from "./settingsCatalog";

describe("Claude unrestricted host access discovery", () => {
  it("finds Assistant harnesses when the operator asks to bypass Claude permissions", () => {
    const text = settingCatalogText(settingCatalogEntry("settings.harnesses")).toLowerCase();
    for (const term of ["unrestricted host access", "claude bypass permissions", "harness permissions"]) expect(text).toContain(term);
    expect(settingCatalog.filter((item) => item.keywords.includes("claude bypass permissions")).map((item) => item.id)).toEqual(["settings.harnesses"]);
  });
});
