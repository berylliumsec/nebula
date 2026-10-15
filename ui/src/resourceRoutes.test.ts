import { describe, expect, it } from "vitest";
import { projectSwitchDestination } from "./resourceRoutes";

describe("project switch destination", () => {
  it("keeps the Workbench view and drops the previous project's saved identities", () => {
    expect(projectSwitchDestination(
      "/projects/old/workbench",
      "?view=chat&session=old-chat&sideChat=old-side&mission=old-run&drawer=results&openFile=old.txt",
      "new project",
    )).toBe("/projects/new%20project/workbench?view=chat");
  });

  it("opens the destination section without an old detail id or filter", () => {
    expect(projectSwitchDestination("/projects/old/findings/old-finding", "?asset=old-asset", "new"))
      .toBe("/projects/new/findings");
    expect(projectSwitchDestination("/projects/old/work/item-1", "?session=old-chat", "new"))
      .toBe("/projects/new/work");
  });
});
