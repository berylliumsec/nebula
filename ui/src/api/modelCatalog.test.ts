import { describe, expect, it } from "vitest";
import { modelCatalogSummary, modelOptionLabel } from "./modelCatalog";

describe("modelOptionLabel", () => {
  it("shows the friendly name, exact request ID, and advertised context", () => {
    expect(modelOptionLabel("anthropic/claude-sonnet-4.5", [{
      id: "anthropic/claude-sonnet-4.5",
      name: "Claude Sonnet 4.5",
      description: null,
      canonicalSlug: "anthropic/claude-sonnet-4.5",
      contextWindow: 200_000,
      maxOutputTokens: 32_000,
      inputModalities: ["text"],
      outputModalities: ["text"],
      supportedParameters: ["tools"],
      pricing: { prompt: "0.000003" },
    }])).toBe("Claude Sonnet 4.5 (anthropic/claude-sonnet-4.5) · 200,000 context");
  });

  it("preserves an unknown saved model ID", () => {
    expect(modelOptionLabel("saved/model", [])).toBe("saved/model");
  });

  it("summarizes advertised controls without claiming verification", () => {
    const descriptor = {
      id: "model",
      name: "Model",
      description: null,
      canonicalSlug: null,
      contextWindow: 64_000,
      maxOutputTokens: 8_192,
      inputModalities: ["text", "image"],
      outputModalities: ["text"],
      supportedParameters: ["tools"],
      pricing: {},
      expirationDate: "2027-06-30T00:00:00Z",
    };
    expect(modelCatalogSummary("model", [descriptor]))
      .toBe("text + image · tools advertised · 8,192 max output · retires 2027-06-30");
  });
});
