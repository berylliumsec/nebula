import { describe, expect, it } from "vitest";
import { settingCatalog, settingCatalogEntry, settingCatalogText } from "./settingsCatalog";
import { settingToOpen } from "./components/nebulaAssistantActions";
import { assistantDestination, assistantDestinationPath, destinationToOpen } from "./assistantDestinations";

describe("Work stages discovery", () => {
  it("names the project stage editor and its Butler actions", () => {
    const entry = settingCatalogEntry("settings.work-stages");
    const text = settingCatalogText(entry).toLowerCase();
    for (const term of ["work stages", "flag when empty", "missing a stage", "group by stage", "nebula.work.set_work_stages"]) expect(text).toContain(term);
    expect(entry.target).toBe("work-stages");
    expect(entry.scope).toBe("Project");
    expect(settingCatalog.filter((item) => item.keywords.includes("work stages")).map((item) => item.id)).toEqual(["settings.work-stages"]);
  });

  it("opens from an explicit request and resolves to the selected project's editor", () => {
    expect(settingToOpen("Open Work stages")?.id).toBe("settings.work-stages");
    expect(settingToOpen("take me to project stages")?.id).toBe("settings.work-stages");
    const destination = destinationToOpen("open work stages");
    expect(destination?.id).toBe("work-stages");
    expect(assistantDestinationPath(assistantDestination("work-stages")!, "p 1")).toBe("/projects/p%201/work?stages=edit");
  });
});
