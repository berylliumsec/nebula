import { describe, expect, it } from "vitest";
import type { HarnessProfile, ProviderHealth } from "../api/types";
import { aiRuntimeOptions } from "./aiRuntimes";

describe("aiRuntimeOptions", () => {
  it("never exposes a stale harness default outside the advertised catalog", () => {
    const harness = {
      id: "grok-harness",
      name: "grok",
      kind: "grok_acp",
      enabled: true,
      localOnly: true,
      permitsSensitiveData: false,
      models: ["grok-4.6", "grok-4.5"],
      defaultModel: "grok-build",
    } as HarnessProfile;

    expect(aiRuntimeOptions([], [harness])[0]).toMatchObject({
      models: ["grok-4.6", "grok-4.5"],
      defaultModel: "grok-4.6",
    });
  });

  it("never exposes a stale provider default outside the advertised catalog", () => {
    const provider = {
      id: "provider-1",
      name: "Local provider",
      enabled: true,
      local: true,
      kind: "local",
      privacy: "local_only",
      permitsSensitiveData: false,
      models: ["current-model"],
      defaultModel: "retired-model",
    } as ProviderHealth;

    expect(aiRuntimeOptions([provider], [])[0]).toMatchObject({
      models: ["current-model"],
      defaultModel: "current-model",
    });
  });
});
