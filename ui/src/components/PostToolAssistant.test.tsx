import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { HarnessProfile, PostToolAssistantConfig } from "../api/types";
import { PostToolAssistant } from "./PostToolAssistant";

const harness: HarnessProfile = {
  id: "harness-1",
  name: "Codex harness",
  kind: "codex_app_server",
  connectionMode: "spawn",
  transport: "stdio",
  authMode: "existing_session",
  defaultModel: "gpt-5-codex",
  models: ["gpt-5-codex"],
  enabled: true,
  localOnly: true,
  permitsSensitiveData: true,
  nativeCapabilities: {
    workspaceAccess: "write",
    shell: true,
    webSearch: true,
    webFetch: false,
    browser: true,
    computerUse: false,
    imageGeneration: false,
    skills: true,
    subagents: true,
  },
  revision: 1,
};

function apiFor(config: PostToolAssistantConfig) {
  let current = config;
  const setPostToolAssistant = vi.fn(async (_engagementId: string, next: PostToolAssistantConfig) => {
    current = next;
    return next;
  });
  const api = {
    getPostToolAssistant: vi.fn(async () => current),
    setPostToolAssistant,
    listPostToolResults: vi.fn().mockResolvedValue([]),
    listExecutions: vi.fn().mockResolvedValue({ items: [] }),
  } as unknown as ApiClient;
  return { api, setPostToolAssistant };
}

describe("PostToolAssistant", () => {
  it("keeps runtime selection out of the Workbench and enables the saved harness", async () => {
    const user = userEvent.setup();
    const config: PostToolAssistantConfig = {
      suggestNextSteps: false,
      takeNotes: false,
      backendKind: "harness",
      harnessProfileId: harness.id,
      model: harness.defaultModel,
      cloudConfirmed: false,
    };
    const { api, setPostToolAssistant } = apiFor(config);
    render(<PostToolAssistant api={api} engagementId="project-1" providers={[]} harnesses={[harness]} onRun={vi.fn()} />);

    const suggestions = await screen.findByRole("checkbox", { name: "Suggest next steps" });
    expect(screen.queryByRole("combobox", { name: /analysis backend/i })).toBeNull();
    await user.click(suggestions);

    await waitFor(() => expect(setPostToolAssistant).toHaveBeenCalledWith("project-1", {
      ...config,
      suggestNextSteps: true,
    }));
    expect(suggestions).toBeChecked();
    const feedback = await screen.findByRole("status");
    expect(feedback).toHaveTextContent("Next-step suggestions enabled");
    expect(feedback).not.toHaveTextContent("Analysis runtime required");
    expect(screen.queryByRole("link", { name: "Open Settings" })).toBeNull();
  });

  it("blocks enablement before runtime setup and explains how to resolve it", async () => {
    const user = userEvent.setup();
    const config: PostToolAssistantConfig = {
      suggestNextSteps: false,
      takeNotes: false,
      backendKind: "harness",
      harnessProfileId: harness.id,
      cloudConfirmed: false,
    };
    const { api, setPostToolAssistant } = apiFor(config);
    render(<PostToolAssistant api={api} engagementId="project-1" providers={[]} harnesses={[harness]} onRun={vi.fn()} />);

    const notes = await screen.findByRole("checkbox", { name: "Take notes" });
    await user.click(notes);

    expect(notes).not.toBeChecked();
    expect(setPostToolAssistant).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Analysis runtime required");
    expect(screen.getByRole("alert")).toHaveTextContent("Choose a model for Codex harness in Settings");
    expect(screen.getByRole("link", { name: "Open Settings" })).toHaveAttribute("href", "/settings#post-tool-assistant-settings");
  });
});
