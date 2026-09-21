import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ProviderHealth } from "../../api/types";
import { defaultSubagentChoice, HarnessSubagentSettings, subagentProviders } from "./HarnessSubagentSettings";

function check(model: string, status: "verified" | "failed") {
  return { [model]: { model, status, checkedAt: "2026-09-21T10:00:00Z", contractVersion: "required-tool-v1" } };
}

function provider(overrides: Partial<ProviderHealth>): ProviderHealth {
  return {
    id: "openrouter",
    revision: 1,
    name: "OpenRouter",
    providerType: "openrouter",
    kind: "gateway",
    local: false,
    state: "healthy",
    enabled: true,
    models: ["deepseek/deepseek-v3.2", "qwen/qwen3-coder"],
    modelAllowlist: [],
    defaultModel: "deepseek/deepseek-v3.2",
    permitsSensitiveData: true,
    autoShareToolResults: false,
    residency: [],
    options: {},
    metadata: {},
    modelCount: 2,
    privacy: "cloud",
    capabilities: ["tools"],
    capabilityVerifications: check("deepseek/deepseek-v3.2", "verified"),
    ...overrides,
  };
}

describe("provider subagents in a harness chat", () => {
  it("offers only providers that may receive project data", () => {
    const closed = provider({ id: "closed", name: "Closed", permitsSensitiveData: false });
    const local = provider({ id: "local", name: "Local", local: true, permitsSensitiveData: false });
    expect(subagentProviders([provider({}), closed, local]).map((item) => item.id)).toEqual(["openrouter", "local"]);
  });

  it("turns on with a checked default model and states where tool outputs go", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const providers = [provider({})];
    const { rerender } = render(<HarnessSubagentSettings
      providers={providers}
      harnessName="Codex"
      choice={{ enabled: false, providerId: "", model: "" }}
      onChange={onChange}
    />);
    expect(screen.getByText(/Codex can hand independent tasks to a provider model/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Subagent model")).not.toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /Provider subagents/ }));
    expect(onChange).toHaveBeenCalledWith({ enabled: true, providerId: "openrouter", model: "deepseek/deepseek-v3.2" });

    rerender(<HarnessSubagentSettings
      providers={providers}
      harnessName="Codex"
      choice={{ enabled: true, providerId: "openrouter", model: "deepseek/deepseek-v3.2" }}
      onChange={onChange}
    />);
    expect(screen.getByLabelText("Subagent model")).toHaveValue("deepseek/deepseek-v3.2");
    expect(screen.getByRole("status")).toHaveTextContent("Tools verified · Subagent tool outputs are sent to OpenRouter.");

    await user.selectOptions(screen.getByLabelText("Subagent model"), "qwen/qwen3-coder");
    expect(onChange).toHaveBeenLastCalledWith({ enabled: true, providerId: "openrouter", model: "qwen/qwen3-coder" });
  });

  it("says a model is being checked, or why it cannot run subagents", () => {
    const failed = provider({ capabilityVerifications: check("qwen/qwen3-coder", "failed") });
    const { rerender } = render(<HarnessSubagentSettings
      providers={[provider({})]}
      harnessName="Codex"
      choice={{ enabled: true, providerId: "openrouter", model: "qwen/qwen3-coder" }}
      onChange={vi.fn()}
    />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking tool support");
    rerender(<HarnessSubagentSettings
      providers={[failed]}
      harnessName="Codex"
      choice={{ enabled: true, providerId: "openrouter", model: "qwen/qwen3-coder" }}
      onChange={vi.fn()}
    />);
    expect(screen.getByRole("alert")).toHaveTextContent("failed the tool check");
  });

  it("prefers a provider whose default model already passed the tool check", () => {
    const unchecked = provider({ id: "first", name: "First", capabilityVerifications: {} });
    expect(defaultSubagentChoice([unchecked, provider({})])).toEqual({ providerId: "openrouter", model: "deepseek/deepseek-v3.2" });
    expect(defaultSubagentChoice([])).toEqual({ providerId: "", model: "" });
  });
});
