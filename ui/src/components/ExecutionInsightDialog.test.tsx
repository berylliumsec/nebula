import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type {
  GeneratedDraft,
  OperatorExecution,
  ProviderHealth,
} from "../api/types";
import { ExecutionInsightDialog } from "./ExecutionInsightDialog";

const execution = { id: "execution-1" } as OperatorExecution;

function provider(
  changes: Partial<ProviderHealth> & Pick<ProviderHealth, "id" | "name">,
): ProviderHealth {
  return {
    revision: 1,
    providerType: "openai",
    kind: "commercial",
    local: false,
    state: "healthy",
    enabled: true,
    models: ["model-1"],
    modelAllowlist: ["model-1"],
    defaultModel: "model-1",
    permitsSensitiveData: true,
    residency: [],
    options: {},
    metadata: {},
    modelCount: 1,
    privacy: "cloud",
    capabilities: ["strict structured output"],
    ...changes,
  };
}

describe("ExecutionInsightDialog", () => {
  it("requires per-request cloud consent and accepts one reviewed draft", async () => {
    const user = userEvent.setup();
    const content = {
      title: "Execution note",
      summary: "Bounded summary",
      observations: ["One observation"],
      potentialFindings: [{ title: "Hypothesis", rationale: "Needs verification" }],
      evidenceIds: ["evidence-1"],
    };
    const draft = {
      id: "draft-1",
      engagementId: "engagement-1",
      executionId: execution.id,
      providerProfileId: "cloud-1",
      model: "model-1",
      promptVersion: "execution-note/v1",
      contextFingerprint: "a".repeat(64),
      status: "ready",
      content,
      metadata: {},
      revision: 1,
    } satisfies GeneratedDraft;
    const generateExecutionDraft = vi.fn().mockResolvedValue(draft);
    const editGeneratedDraft = vi.fn().mockResolvedValue({ ...draft, revision: 2 });
    const transitionGeneratedDraft = vi
      .fn()
      .mockResolvedValue({ ...draft, revision: 3, status: "accepted" });
    const api = {
      generateExecutionDraft,
      editGeneratedDraft,
      transitionGeneratedDraft,
    } as unknown as ApiClient;
    const onClose = vi.fn();

    render(
      <ExecutionInsightDialog
        action="draft"
        api={api}
        execution={execution}
        providers={[
          provider({ id: "cloud-1", name: "Cloud provider" }),
          provider({
            id: "prose-only",
            name: "Prose only",
            capabilities: ["chat"],
          }),
        ]}
        onClose={onClose}
        onChatAttached={vi.fn()}
      />,
    );

    expect(screen.getByText("Up to 32 KiB of bounded redacted source")).toBeVisible();
    expect(screen.getByRole("button", { name: "Generate draft" })).toBeDisabled();
    expect(screen.queryByRole("option", { name: /Prose only/ })).toBeNull();

    await user.click(screen.getByRole("checkbox", { name: /Allow this cloud request/ }));
    await user.click(screen.getByRole("button", { name: "Generate draft" }));
    await screen.findByText(/Accepting creates one observation/);
    expect(generateExecutionDraft).toHaveBeenCalledWith(
      execution.id,
      "cloud-1",
      "model-1",
      true,
    );
    expect(screen.getByText("Unverified hypothesis 1")).toBeVisible();

    await user.click(screen.getByRole("button", { name: /Accept as observation/ }));
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(editGeneratedDraft).toHaveBeenCalledWith("draft-1", content, 1);
    expect(transitionGeneratedDraft).toHaveBeenCalledWith("draft-1", "accept", 2);
  });
});
