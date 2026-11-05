import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { HarnessProfile, PostToolAssistantConfig } from "../api/types";
import { PostToolAssistant } from "./PostToolAssistant";

const harness: HarnessProfile = {
  permissionMode: "managed",
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
  autoShareToolResults: false,
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
    listRuns: vi.fn().mockResolvedValue({ items: [] }),
  } as unknown as ApiClient;
  return { api, setPostToolAssistant };
}

describe("PostToolAssistant", () => {
  it.each(["execution", "mission"])("explicitly requests automatic notes for a completed %s", async kind => {
    const config: PostToolAssistantConfig = {suggestNextSteps: false, takeNotes: true, backendKind: "harness",
      harnessProfileId: harness.id, model: harness.defaultModel, cloudConfirmed: false};
    const {api} = apiFor(config);
    vi.mocked(api.listExecutions).mockResolvedValue({items: kind === "execution" ? [{id: "finished", status: "completed",
      queuedAt: "2026-11-02T00:00:00Z"}] : [], total: kind === "execution" ? 1 : 0} as Awaited<ReturnType<ApiClient["listExecutions"]>>);
    vi.mocked(api.listRuns).mockResolvedValue({items: kind === "mission" ? [{id: "finished", status: "complete",
      updatedAt: "2026-11-02T00:00:00Z"}] : [], total: kind === "mission" ? 1 : 0} as Awaited<ReturnType<ApiClient["listRuns"]>>);
    const generateExecutionDraft = vi.fn().mockResolvedValue({id: "saved", status: "accepted"});
    const generateMissionDraft = vi.fn().mockResolvedValue({id: "saved", status: "accepted"});
    Object.assign(api, {generateExecutionDraft, generateMissionDraft});
    const view = render(<PostToolAssistant api={api} engagementId="project-1" providers={[]} harnesses={[harness]} onRun={vi.fn()} />);
    await waitFor(() => expect(kind === "execution" ? generateExecutionDraft : generateMissionDraft).toHaveBeenCalledWith(
      "finished", "harness", harness.defaultModel, false, config, true));
    expect(kind === "execution" ? generateMissionDraft : generateExecutionDraft).not.toHaveBeenCalled();
    view.unmount();
  });

  it.each(["icon", "panel"] as const)("keeps runtime selection out of the %s controls and enables the saved harness", async triggerVariant => {
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
    render(<PostToolAssistant api={api} engagementId="project-1" providers={[]} harnesses={[harness]} onRun={vi.fn()} triggerVariant={triggerVariant} />);

    if (triggerVariant === "icon") {
      const opener = screen.getByRole("button", { name: "Tool assistance" });
      expect(opener).toHaveAttribute("data-guide", "tool-assistance");
      await user.click(opener);
    } else {
      expect(screen.getByRole("group", { name: "Post-tool assistance" })).toHaveAttribute("data-guide", "tool-assistance");
    }
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

  it("polls finished work every few seconds instead of continuously", async () => {
    const config: PostToolAssistantConfig = {
      suggestNextSteps: true,
      takeNotes: false,
      backendKind: "harness",
      harnessProfileId: harness.id,
      model: harness.defaultModel,
      cloudConfirmed: false,
    };
    const { api } = apiFor(config);
    // Core answers every read with a fresh object, as any JSON client does.
    (api.getPostToolAssistant as ReturnType<typeof vi.fn>).mockImplementation(async () => ({ ...config }));
    render(<PostToolAssistant api={api} engagementId="project-1" providers={[]} harnesses={[harness]} onRun={vi.fn()} />);

    await waitFor(() => expect(api.listExecutions).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 300));

    expect(api.listExecutions).toHaveBeenCalledTimes(1);
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

    await user.click(screen.getByRole("button", { name: "Tool assistance" }));
    const notes = await screen.findByRole("checkbox", { name: "Take notes" });
    await user.click(notes);

    expect(notes).not.toBeChecked();
    expect(setPostToolAssistant).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Analysis runtime required");
    expect(screen.getByRole("alert")).toHaveTextContent("Choose a model for Codex harness in Settings");
    expect(screen.getByRole("link", { name: "Open Settings" })).toHaveAttribute("href", "/settings#post-tool-assistant-settings");
  });
});
