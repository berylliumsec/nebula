import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { ExecutionCapabilities, ExecutionPreflight } from "../api/types";
import { ExecutionReviewDialog } from "./ExecutionReviewDialog";

const capabilities: ExecutionCapabilities = {
  engagementId: "project-1",
  ready: true,
  runtimes: [{
    language: "bash",
    aliases: ["bash", "shell"],
    offline: true,
    scopedNetwork: true,
    unrestrictedNetwork: true,
  }],
  limits: { cpuCount: 1, memoryMb: 512, pids: 64, timeoutSeconds: 30, outputBytesPerStream: 262_144 },
  workspace: "/workspace",
};

describe("ExecutionReviewDialog", () => {
  it("reviews unrestricted networking without asking for a target or ports", async () => {
    const preview: ExecutionPreflight = {
      allowed: true,
      detail: "Reviewed execution permitted.",
      network: { mode: "unrestricted", ports: [], resolvedAddresses: [] },
      limits: capabilities.limits,
      workspace: "/workspace",
      previewToken: "preview-token",
      previewFingerprint: "a".repeat(64),
    };
    const preflightExecution = vi.fn().mockResolvedValue(preview);

    render(<ExecutionReviewDialog
      api={{ preflightExecution } as unknown as ApiClient}
      engagementId="project-1"
      candidate={{
        language: "bash",
        declaredLanguage: "bash",
        source: "curl https://api.ipify.org\n",
        origin: { kind: "selection", sourceKind: "editor", sourceId: "buffer-1", sourceSha256: "b".repeat(64) },
      }}
      capabilities={capabilities}
      onClose={vi.fn()}
      onStarted={vi.fn()}
    />);

    expect(screen.getByText("Unrestricted outbound access", { exact: false })).toBeVisible();
    expect(screen.queryByText("One scoped target")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("host.example or 192.0.2.10")).not.toBeInTheDocument();
    await waitFor(() => expect(preflightExecution).toHaveBeenCalledWith(
      expect.objectContaining({ network: { mode: "unrestricted", ports: [] } }),
      expect.any(AbortSignal),
    ));
  });
});
