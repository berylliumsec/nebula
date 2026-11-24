import { describe, expect, it } from "vitest";
import { assistantDestination, destinationToOpen } from "./assistantDestinations";

describe("Butler board stage filter discovery", () => {
  it("opens Butler mode for a request to filter the board by stage", () => {
    expect(destinationToOpen("open the board stage filter")?.id).toBe("butler-mode");
    expect(destinationToOpen("show me goals by stage")?.id).toBe("butler-mode");
    const guide = (assistantDestination("butler-mode") as unknown as { guide?: string }).guide ?? "";
    expect(guide).toContain("Stage (beside Group) filters the board");
    expect(guide).toContain("Hold and Resume act only on the filtered chats");
  });
});
