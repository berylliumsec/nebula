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
  expect(pairing).toContain("project board");
  expect(pairing).toContain("/board");
  expect(pairing).toContain("work check-ins");
  for (const phrase of ["/board new", "/board update", "track this conversation", "link work item to chat", "agent work tools"]) {
    expect(pairing).toContain(phrase);
  }
  expect(pairing).toContain("project notes");
  expect(pairing).toContain("/notes");
  expect(pairing).toContain("up arrow message recall");
  expect(pairing).toContain("last sent message");
  expect(pairing).toContain("multi-select skills");
  expect(pairing).toContain("/goal skills");
  expect(pairing).toContain("mcp server picker");
  expect(pairing).toContain("/mcp");
  for (const command of ["/thinking", "/chat edit", "/chat bulk", "/chat finish", "/search", "/bookmark", "/side"]) {
    expect(pairing).toContain(command);
  }
  expect(pairing).toContain("turn elapsed time");
  expect(pairing).toContain("token usage");
});
