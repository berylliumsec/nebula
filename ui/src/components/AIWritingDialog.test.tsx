import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { HarnessProfile, ProviderHealth, WritingTransformResponse } from "../api/types";
import { AIWritingDialog } from "./AIWritingDialog";

const provider = {
  id: "provider-1",
  name: "Local writer",
  enabled: true,
  local: true,
  kind: "local",
  privacy: "local_only",
  permitsSensitiveData: false,
  models: ["model-1"],
  defaultModel: "model-1",
} as ProviderHealth;

const harness = {
  id: "harness-1",
  name: "Local Codex",
  kind: "codex_app_server",
  enabled: true,
  localOnly: true,
  permitsSensitiveData: false,
  models: ["codex-model"],
  defaultModel: "codex-model",
} as HarnessProfile;

const response: WritingTransformResponse = {
  content: "Generated report prose.",
  provenance: {
    providerProfileId: "provider-1",
    model: "model-1",
    promptVersion: "writing-transform/v1",
    sourceSha256: "a".repeat(64),
    instruction: "Make it concise.",
    generatedAt: "2026-07-15T12:00:00Z",
  },
  usage: { inputTokens: 10, outputTokens: 5, totalTokens: 15 },
};

describe("AIWritingDialog", () => {
  it("generates an editable draft and applies only the reviewed text", async () => {
    const user = userEvent.setup();
    const transformWriting = vi.fn().mockResolvedValue(response);
    const onApply = vi.fn();
    render(<AIWritingDialog
      api={{ transformWriting } as unknown as ApiClient}
      engagementId="engagement-1"
      providers={[provider]}
      purpose="report_summary"
      title="Draft report"
      description="Draft from selected content."
      sourceLabel="Report context"
      sourceText="One validated medium finding."
      initialInstruction="Make it concise."
      onApply={onApply}
      onClose={vi.fn()}
    />);

    await user.click(screen.getByRole("button", { name: "Generate draft" }));
    await waitFor(() => expect(transformWriting).toHaveBeenCalledWith(expect.objectContaining({
      engagementId: "engagement-1",
      providerId: "provider-1",
      model: "model-1",
      purpose: "report_summary",
      sourceText: "One validated medium finding.",
    }), expect.any(AbortSignal)));
    const draft = await screen.findByRole("textbox", { name: "AI writing draft" });
    await user.clear(draft);
    await user.type(draft, "Reviewed and edited report prose.");
    await user.click(screen.getByRole("button", { name: "Apply draft" }));

    expect(onApply).toHaveBeenCalledWith(expect.objectContaining({
      content: "Reviewed and edited report prose.",
      provenance: response.provenance,
    }));
  });

  it("uses a Codex harness when no model provider is configured", async () => {
    const user = userEvent.setup();
    const transformWriting = vi.fn().mockResolvedValue({
      ...response,
      provenance: {
        ...response.provenance,
        providerProfileId: harness.id,
        model: "codex-model",
      },
    });
    render(<AIWritingDialog
      api={{ transformWriting } as unknown as ApiClient}
      engagementId="engagement-1"
      providers={[]}
      harnesses={[harness]}
      purpose="note"
      title="Transform note"
      description="Transform with Codex."
      sourceLabel="Analyst note"
      sourceText="Port 443 was open."
      initialInstruction="Make it concise."
      onApply={vi.fn()}
      onClose={vi.fn()}
    />);

    expect(screen.getByRole("option", { name: /Local Codex · Codex/ })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Generate draft" }));

    await waitFor(() => expect(transformWriting).toHaveBeenCalledWith(
      expect.objectContaining({
        backendKind: "harness",
        providerId: undefined,
        harnessProfileId: "harness-1",
        model: "codex-model",
      }),
      expect.any(AbortSignal),
    ));
  });
});
