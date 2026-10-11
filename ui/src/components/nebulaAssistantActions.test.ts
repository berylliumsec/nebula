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
    expect(settingToOpen("open project scope")?.id).toBe("settings.network-scope");
    expect(settingToOpen("What can I change in settings?")).toBeUndefined();
    expect(settingToOpen("open assistant")).toBeUndefined();
  });

  it("routes plain-language requests for every catalog setting without a model turn", () => {
    const cases = [
      ["open theme", "settings.appearance"],
      ["show provider", "settings.providers"],
      ["bring up playbook", "settings.skills"],
      ["open internet", "settings.web-search"],
      ["show follow up", "settings.follow-up"],
      ["open codex", "settings.harnesses"],
      ["open mcp", "settings.mcp"],
      ["open ssh", "settings.ssh-environments"],
      ["open automation", "settings.automation-runtime"],
      ["open vpn", "settings.vpn-egress"],
      ["open podman", "settings.runners"],
      ["open project scope", "settings.network-scope"],
      ["open identity", "settings.operators"],
      ["open qr", "settings.devices"],
      ["open upgrade", "settings.release"],
      ["open telemetry", "settings.diagnostics"],
      ["open readiness", "settings.setup"],
    ];
    for (const [prompt, expected] of cases) expect(settingToOpen(prompt)?.id, prompt).toBe(expected);
  });

  it("finds the intended setting in conversational navigation requests", () => {
    const cases = [
      ["where can I change the theme colors?", "settings.appearance"],
      ["which setting holds API keys for models?", "settings.providers"],
      ["where are the shared skills?", "settings.skills"],
      ["where can I find web research?", "settings.web-search"],
      ["where is automatic follow up?", "settings.follow-up"],
      ["where do I configure the harness?", "settings.harnesses"],
      ["where can I add an MCP server?", "settings.mcp"],
      ["which setting manages remote SSH machines?", "settings.ssh-environments"],
      ["where is the automation runtime?", "settings.automation-runtime"],
      ["where do I upload an OpenVPN tunnel profile?", "settings.vpn-egress"],
      ["where is the Podman runner config?", "settings.runners"],
      ["where is project scope for CIDR ports?", "settings.network-scope"],
      ["where can I set operator identity?", "settings.operators"],
      ["where can I pair my phone?", "settings.devices"],
      ["where are release channel updates?", "settings.release"],
      ["where do I inspect logs?", "settings.diagnostics"],
      ["where can I check Core readiness?", "settings.setup"],
    ];
    for (const [prompt, expected] of cases) expect(settingToOpen(prompt)?.id, prompt).toBe(expected);
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
