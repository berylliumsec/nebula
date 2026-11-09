import { expect, it } from "vitest";
import { settingCatalogEntry, settingCatalogText } from "./settingsCatalog";

it("distinguishes closing the desktop from explicit Core shutdown in product discovery", () => {
  const setup = settingCatalogEntry("settings.setup");
  expect(setup.target).toBe("setup-settings");
  expect(settingCatalogText(setup).toLowerCase()).toContain("shut down core");
  expect(setup.description).toContain("keeps Core and work running");
});
