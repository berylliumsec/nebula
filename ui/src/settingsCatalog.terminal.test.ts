import { expect, it } from "vitest";
import { settingCatalogEntry, settingCatalogText } from "./settingsCatalog";

it("discovers the paired terminal TUI and live agent progress", () => {
  const setup = settingCatalogEntry("settings.setup");
  const text = settingCatalogText(setup).toLowerCase();
  expect(text).toContain("terminal tui");
  expect(text).toContain("live agent progress");
  expect(text).toContain("nebula-chat");
  const pairing = settingCatalogText(settingCatalogEntry("settings.devices")).toLowerCase();
  expect(pairing).toContain("running conversation");
  expect(pairing).toContain("idle conversation");
  expect(pairing).toContain("selection feedback");
  expect(pairing).toContain("animated spinner");
  expect(pairing).toContain("reduced motion");
  expect(pairing).toContain("project snapshot");
  expect(pairing).toContain("/snapshot");
});
