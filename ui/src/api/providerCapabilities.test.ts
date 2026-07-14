import { describe, expect, it } from "vitest";
import { providerVerificationModel } from "./providerCapabilities";

describe("providerVerificationModel", () => {
  it("prefers a configured default and falls back to allowed or discovered models", () => {
    expect(providerVerificationModel({
      defaultModel: " configured ",
      modelAllowlist: ["allowed"],
      models: ["discovered"],
    })).toBe("configured");
    expect(providerVerificationModel({
      modelAllowlist: ["allowed"],
      models: ["discovered"],
    })).toBe("allowed");
    expect(providerVerificationModel({
      modelAllowlist: [],
      models: ["discovered"],
    })).toBe("discovered");
  });

  it("returns no target when health discovery and configuration are empty", () => {
    expect(providerVerificationModel({ modelAllowlist: [], models: [] })).toBeUndefined();
  });
});
