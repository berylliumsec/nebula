import { describe, expect, it } from "vitest";
import type { HarnessProfile } from "../api/types";
import { assistantTurnOptions, projectModeChange, settingToOpen } from "./nebulaAssistantActions";

describe("settings assistant actions", () => {
  it("recognizes explicit project mode changes without treating questions as commands", () => {
    expect(projectModeChange("open project policy and change from dockermode to host mode"))
      .toEqual({ from: "docker", mode: "host" });
    expect(projectModeChange("switch project execution mode to Docker"))
      .toEqual({ mode: "docker" });
    expect(projectModeChange("Where is Host mode?")).toBeUndefined();
    expect(projectModeChange("open project policy")).toBeUndefined();
  });
  it("opens an unambiguous named setting and ignores ordinary questions", () => {
    expect(settingToOpen("Please open MCP servers settings")?.id).toBe("settings.mcp");
    expect(settingToOpen("can you bring up model provider settings?")?.id).toBe("settings.providers");
    expect(settingToOpen("What can I change in settings?")).toBeUndefined();
    expect(settingToOpen("open assistant")).toBeUndefined();
  });

  it("chooses advertised low effort and a matching fast harness model", () => {
    const harness = {
      id: "grok", models: ["grok-4.7", "grok-4.7-build-fast"], modelOptions: [
        { model: "grok-4.7", reasoningEfforts: [{ id: "low" }], serviceTiers: [] },
        { model: "grok-4.7-build-fast", reasoningEfforts: [{ id: "low" }], serviceTiers: [] },
      ],
    } as unknown as HarnessProfile;
    expect(assistantTurnOptions({ kind: "harness", id: "grok", model: "grok-4.7" }, [harness])).toEqual({
      model: "grok-4.7-build-fast", harnessReasoningEffort: "low",
    });
  });

  it("keeps the current model when it advertises a fast tier", () => {
    const harness = {
      id: "codex", models: ["model", "model-build-fast"], modelOptions: [
        { model: "model", reasoningEfforts: [{ id: "low" }], serviceTiers: [{ id: "fast" }] },
      ],
    } as unknown as HarnessProfile;
    expect(assistantTurnOptions({ kind: "harness", id: "codex", model: "model" }, [harness])).toEqual({
      model: "model", harnessReasoningEffort: "low", harnessServiceTier: "fast",
    });
  });

  it("requests low effort for providers without a fast-mode selection", () => {
    expect(assistantTurnOptions({ kind: "provider", id: "provider", model: "reasoning" }, []))
      .toEqual({ model: "reasoning", reasoningEffort: "low" });
    expect(assistantTurnOptions({ kind: "provider", id: "provider", model: "plain" }, []))
      .toEqual({ model: "plain", reasoningEffort: "low" });
  });
});
