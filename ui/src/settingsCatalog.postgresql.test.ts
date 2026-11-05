import { expect, it } from "vitest";
import { settingCatalogEntry, settingCatalogText } from "./settingsCatalog";

it("discovers PostgreSQL migration guidance from diagnostics", () => {
  const text = settingCatalogText(settingCatalogEntry("settings.diagnostics")).toLowerCase();
  expect(text).toContain("postgresql");
  expect(text).toContain("sqlite");
  expect(text).toContain("migrate-to-postgresql");
});
