import { describe, expect, it } from "vitest";
import { settingNavigationCandidates } from "./components/nebulaAssistantActions";
import { settingCatalogEntry, settingCatalogText } from "./settingsCatalog";

describe("OpenAI provider settings discovery", () => {
  it("routes API key and Daybreak Blue questions to Model providers", () => {
    const entry = settingCatalogEntry("settings.providers");
    expect(entry.target).toBe("provider-settings");
    expect(settingCatalogText(entry).toLowerCase()).toContain("daybreak blue");
    expect(settingNavigationCandidates("Where are the OpenAI API key and Daybreak Blue settings?")[0]?.id).toBe("settings.providers");
  });
});
