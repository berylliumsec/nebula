import { describe, expect, it } from "vitest";
import { subagentRequestFields } from "./chatSubagentChoice";

const choice = { enabled: true, ready: true, providerId: "openrouter", model: "deepseek/deepseek-v3.2", limit: 2 };

describe("the subagent fields a message carries", () => {
  it("delegates a provider chat to its own model, with the operator's limit", () => {
    expect(subagentRequestFields("provider", choice)).toEqual({ allowSubagents: true, maxActiveSubagents: 2 });
    expect(subagentRequestFields("provider", { ...choice, enabled: false })).toEqual({ allowSubagents: false, maxActiveSubagents: undefined });
  });

  it("runs a harness chat's subagents on the chosen model once it is verified", () => {
    expect(subagentRequestFields("harness", choice)).toEqual({
      allowSubagents: true,
      subagentProviderId: "openrouter",
      subagentModel: "deepseek/deepseek-v3.2",
      maxActiveSubagents: 2,
    });
  });

  it("carries a harness choice still being verified for Core to remember, without using it", () => {
    expect(subagentRequestFields("harness", { ...choice, ready: false })).toEqual({
      allowSubagents: false,
      pendingProviderSubagent: { providerId: "openrouter", model: "deepseek/deepseek-v3.2", maxActive: 2 },
    });
    // Checked before a model is picked is still a choice to keep.
    expect(subagentRequestFields("harness", { enabled: true, ready: false, providerId: "", model: "" })).toEqual({
      allowSubagents: false,
      pendingProviderSubagent: { providerId: "", model: "", maxActive: undefined },
    });
  });

  it("sends nothing to remember when Subagents is off", () => {
    expect(subagentRequestFields("harness", { ...choice, enabled: false, ready: false })).toEqual({ allowSubagents: false });
  });
});
