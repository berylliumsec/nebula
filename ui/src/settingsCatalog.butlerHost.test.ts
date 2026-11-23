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
