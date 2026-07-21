import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ProviderHealth } from "../api/types";
import { ProviderHealthCard } from "./ProviderHealthCard";

const discoveredProvider: ProviderHealth = {
  id: "provider-1",
  revision: 1,
  name: "Local vLLM",
  providerType: "vllm",
  kind: "local",
  local: true,
  state: "healthy",
  enabled: true,
  endpoint: "http://127.0.0.1:8001/v1",
  models: ["Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"],
  modelAllowlist: [],
  permitsSensitiveData: false,
  residency: [],
  options: {},
  metadata: {},
  modelCount: 1,
  privacy: "local_only",
  capabilities: ["streaming"],
  capabilityVerifications: {},
};

describe("ProviderHealthCard", () => {
  it("lets an operator verify a model discovered by provider health", async () => {
    const onReverify = vi.fn(async () => undefined);
    const user = userEvent.setup();
    render(<ProviderHealthCard provider={discoveredProvider} onReverify={onReverify} />);

    expect(screen.getByText("Tool calling is unverified for Qwen/Qwen2.5-Coder-7B-Instruct-AWQ.")).toBeVisible();
    const button = screen.getByRole("button", { name: "Reverify Local vLLM tool calling" });
    expect(button).toBeEnabled();
    expect(button).toHaveAttribute("title", "Verify tool calling for Qwen/Qwen2.5-Coder-7B-Instruct-AWQ");

    await user.click(button);
    expect(onReverify).toHaveBeenCalledWith("provider-1");
  });

  it("keeps verification disabled until a model is configured or discovered", () => {
    render(<ProviderHealthCard provider={{ ...discoveredProvider, models: [], modelCount: 0 }} onReverify={vi.fn()} />);
    expect(screen.getByText("Configure a model to verify tool calling.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Reverify Local vLLM tool calling" })).toBeDisabled();
  });
});
