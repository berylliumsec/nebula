import { describe, expect, it } from "vitest";
import { providerModelVerification, providerVerificationModel } from "./providerCapabilities";

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

describe("providerModelVerification", () => {
  const verification = {
    model: "model-1",
    status: "verified" as const,
    checkedAt: "2026-07-13T00:00:00Z",
    contractVersion: "required-tool-v1",
  };

  it("matches only the exact selected model while tolerating surrounding whitespace", () => {
    const provider = { capabilityVerifications: { " model-1 ": verification } };
    expect(providerModelVerification(provider, " model-1 ")).toEqual(verification);
    expect(providerModelVerification(provider, "model-2")).toBeUndefined();
  });
});
