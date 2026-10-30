import { describe, expect, it } from "vitest";
import { assistantDestinations, assistantDestinationPath, destinationToOpen } from "./assistantDestinations";

describe("assistant app destinations", () => {
  it("opens All projects from the operator's wording without a model turn", () => {
    expect(destinationToOpen("show me my projects")?.id).toBe("projects");
    expect(destinationToOpen("open all projects")?.id).toBe("projects");
    expect(destinationToOpen("open Workbench")?.id).toBe("chat");
    expect(destinationToOpen("show Butler conversations")?.id).toBe("chat");
    expect(destinationToOpen("show archived Butler chats")?.id).toBe("chat");
    expect(destinationToOpen("open pinned chats")?.id).toBe("chat");
    expect(destinationToOpen("show flagged chats")?.id).toBe("chat");
    expect(destinationToOpen("open bulk chat actions")?.id).toBe("chat");
    expect(destinationToOpen("open select conversations")?.id).toBe("chat");
    expect(destinationToOpen("open goal resume instruction")?.id).toBe("chat");
    expect(destinationToOpen("open switch chat provider")?.id).toBe("chat");
    expect(destinationToOpen("open check-in history")?.id).toBe("project-work");
    expect(destinationToOpen("open Knowledge")?.id).toBe("sources");
    expect(destinationToOpen("show storage devices")?.id).toBe("storage");
    expect(destinationToOpen("How do projects work?")).toBeUndefined();
  });

  it("resolves every listed route with the selected project and preserves encoded IDs", () => {
    expect(assistantDestinations).toHaveLength(22);
    expect(destinationToOpen("open Butler scheduled activity")?.path).toBe("/settings#butler-oversight");
    expect(new Set(assistantDestinations.map((entry) => entry.id)).size).toBe(assistantDestinations.length);
    for (const destination of assistantDestinations) {
      expect(destinationToOpen(`open ${destination.label}`)?.id, destination.id).toBe(destination.id);
      const path = assistantDestinationPath(destination, "project / one");
      expect(path?.startsWith("/"), destination.id).toBe(true);
      expect(path).not.toContain("{project_id}");
      if (destination.path.includes("{project_id}")) {
        expect(path).toContain("/projects/project%20%2F%20one");
        expect(assistantDestinationPath(destination)).toBeUndefined();
      }
    }
  });
});
