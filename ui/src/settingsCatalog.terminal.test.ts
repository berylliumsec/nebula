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
  for (const phrase of ["/queue", "/now", "stop and send", "/mcp exclude", "project defaults for new chats", "always allow tool results", "resume harness session"]) {
    expect(pairing).toContain(phrase);
  }
  for (const phrase of ["/goal complete", "/goal child", "subagent approvals", "edit pending request", "always allow on this host", "stop all subagents"]) {
    expect(pairing).toContain(phrase);
  }
  for (const phrase of ["/project", "rename project", "relink project folder", "restore archived project", "delete archived project", "project execution mode", "project network scope", "project vpn route", "/project followup", "after-tool assistant"]) {
    expect(pairing).toContain(phrase);
  }
  expect(pairing).toContain("type ahead");
  expect(pairing).toContain("multi-word picker search");

  for (const reading of ["/catchup", "/quote", "/evidence", "/turn", "terminal catch-up", "supporting evidence", "turn details"]) {
    expect(pairing).toContain(reading);
  }
  for (const inspector of ["artifact inspector", "tool output", "/results", "stop harness subagent", "checkpoint rewind"]) {
    expect(pairing).toContain(inspector);
  }
});
