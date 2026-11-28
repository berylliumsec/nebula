import { describe, expect, it } from "vitest";
import { settingCatalog, settingCatalogEntry, settingCatalogText } from "./settingsCatalog";

describe("Nebula-wide Butler host discovery", () => {
  it("finds Butler's instructions when the operator asks why Butler needs an Act host", () => {
    const entry = settingCatalogEntry("settings.butler-instructions");
    const text = settingCatalogText(entry).toLowerCase();
    for (const term of ["needs act", "host", "scheduled", "act"]) expect(text).toContain(term);
    expect(settingCatalog.filter((item) => item.keywords.includes("needs act")).map((item) => item.id)).toEqual(["settings.butler-instructions"]);
  });
});

describe("Butler check instructions discovery", () => {
  it("finds Butler's instructions when the operator asks what event checks are told", () => {
    const text = settingCatalogText(settingCatalogEntry("settings.butler-instructions")).toLowerCase();
    for (const term of ["event checks", "check instructions"]) expect(text).toContain(term);
  });
});
