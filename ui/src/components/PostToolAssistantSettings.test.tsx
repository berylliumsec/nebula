import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { HarnessProfile, PostToolAssistantConfig } from "../api/types";
import { PostToolAssistantSettings } from "./PostToolAssistantSettings";

const harnessWithoutDiscoveredModel: HarnessProfile = {
  id: "harness-1",
  name: "Codex harness",
  kind: "codex_app_server",
  connectionMode: "spawn",
  transport: "stdio",
  authMode: "existing_session",
  models: [],
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

describe("PostToolAssistantSettings", () => {
  it("persists an explicit harness model without enabling either Workbench control", async () => {
    const user = userEvent.setup();
    const initial: PostToolAssistantConfig = {
      suggestNextSteps: false,
      takeNotes: false,
      backendKind: "provider",
      cloudConfirmed: false,
    };
    const setPostToolAssistant = vi.fn(async (_engagementId: string, config: PostToolAssistantConfig) => config);
    const api = {
      getPostToolAssistant: vi.fn().mockResolvedValue(initial),
      listHarnesses: vi.fn().mockResolvedValue([harnessWithoutDiscoveredModel]),
      setPostToolAssistant,
    } as unknown as ApiClient;
    render(<PostToolAssistantSettings api={api} engagementId="project-1" providers={[]} />);

    const runtime = await screen.findByRole("combobox", { name: "Tool follow-up runtime" });
    await user.selectOptions(runtime, "harness:harness-1");
    await user.type(screen.getByLabelText("Tool follow-up model"), "gpt-5-codex");
    await user.click(screen.getByRole("button", { name: "Save runtime" }));

    await waitFor(() => expect(setPostToolAssistant).toHaveBeenCalledWith("project-1", {
      suggestNextSteps: false,
      takeNotes: false,
      backendKind: "harness",
      harnessProfileId: "harness-1",
      providerId: undefined,
      model: "gpt-5-codex",
      cloudConfirmed: false,
    }));
    expect(await screen.findByText("Ready")).toBeVisible();
  });
});
