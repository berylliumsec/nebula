import { expect, it } from "vitest";
import { destinationToOpen } from "./assistantDestinations";
import { settingCatalogEntry, settingCatalogText } from "./settingsCatalog";

it("lets the settings assistant discover goal closure review", () => {
  const text = settingCatalogText(settingCatalogEntry("settings.harnesses")).toLowerCase();
  expect(text).toContain("goal closure review");
  expect(text).toContain("hold completion for review");
  // The choice lives on each goal, so the destination is the chat's goal editor.
  expect(destinationToOpen("open goal closure review")?.id).toBe("chat");
  expect(destinationToOpen("open approve goal completion")?.id).toBe("chat");
});
