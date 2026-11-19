import { describe, expect, it } from "vitest";
import { settingCatalog, settingCatalogEntry, settingCatalogText } from "./settingsCatalog";
import { settingToOpen } from "./components/nebulaAssistantActions";

describe("Host command queue discovery", () => {
  it("leads an operator whose command waits for Host capacity to the shared queue", () => {
    const entry = settingCatalogEntry("settings.host-commands");
    const text = settingCatalogText(entry).toLowerCase();
    for (const term of ["host commands", "running at once", "command queue", "waiting for host capacity", "cancel command", "stop command", "detached"]) {
      expect(text).toContain(term);
    }
    expect(entry.target).toBe("host-command-settings");
    expect(entry.category).toBe("Automation");
  });
  it("is the only destination named by its queue terms", () => {
    for (const term of ["host commands running at once", "waiting for host capacity", "command queue"]) {
      expect(settingCatalog.filter((item) => item.keywords.includes(term)).map((item) => item.id)).toEqual(["settings.host-commands"]);
    }
  });
  it("opens from an explicit assistant navigation request", () => {
    expect(settingToOpen("Open Host commands settings")?.id).toBe("settings.host-commands");
    expect(settingToOpen("take me to the command queue")?.id).toBe("settings.host-commands");
    expect(settingToOpen("where do I set Host commands running at once?")?.id).toBe("settings.host-commands");
  });
});
