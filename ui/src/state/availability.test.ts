import { describe, expect, it } from "vitest";
import { projectAvailability, terminalAvailability } from "./availability";

describe("feature availability", () => {
  it("provides an actionable project prerequisite", () => {
    expect(projectAvailability()).toMatchObject({
      state: "blocked",
      reasonCode: "project_required",
      recovery: { id: "create_project" },
    });
  });

  it("routes terminal failures to verified setup", () => {
    expect(terminalAvailability("project", "needs_runner")).toMatchObject({
      state: "blocked",
      recovery: { destination: "/settings#setup-settings" },
    });
  });
});
