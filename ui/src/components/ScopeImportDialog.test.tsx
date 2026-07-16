import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type {
  EngagementScopePolicy,
  ProviderHealth,
  ScopeImport,
} from "../api/types";
import { ScopeImportDialog } from "./ScopeImportDialog";

const scope: EngagementScopePolicy = {
  id: "scope:eng-1",
  engagementId: "eng-1",
  allowedCidrs: [],
  allowedDomains: [],
  allowedUrls: [],
  allowedPorts: [443],
  prohibitedActions: [],
  localOnly: true,
  maxConcurrency: 1,
  grants: [],
  revision: 4,
};

const provider: ProviderHealth = {
  id: "provider-1",
  revision: 1,
  name: "Local structured model",
  providerType: "vllm",
  kind: "local",
  local: true,
  state: "healthy",
  enabled: true,
  models: ["model-1"],
  modelAllowlist: ["model-1"],
  permitsSensitiveData: true,
  residency: [],
  options: {},
  metadata: {},
  modelCount: 1,
  privacy: "local_only",
  capabilities: ["strict structured output"],
};

const imported: ScopeImport = {
  id: "import-1",
  engagementId: "eng-1",
  artifactId: "artifact-1",
  filename: "scope.csv",
  sourceType: "csv",
  sourceSha256: "a".repeat(64),
  baseScopeRevision: 4,
  status: "ready",
  candidates: [
    {
      id: "allowed",
      targetType: "cidr",
      classification: "allowed",
      rawValue: "192.0.2.7",
      normalizedValue: "192.0.2.7/32",
      sourceLocation: "row 2",
      sourceExcerpt: "In scope",
      warnings: [],
    },
    {
      id: "excluded",
      targetType: "domain",
      classification: "excluded",
      rawValue: "admin.example.test",
      normalizedValue: "admin.example.test",
      sourceLocation: "row 3",
      sourceExcerpt: "Excluded",
      warnings: [],
    },
  ],
  warnings: [],
  usage: { inputTokens: 10, outputTokens: 5, totalTokens: 15 },
  appliedCandidateIds: [],
  revision: 2,
};

describe("ScopeImportDialog", () => {
  it("requires per-entry review and applies only selectable allowed targets", async () => {
    const createScopeImport = vi.fn().mockResolvedValue(imported);
    const applyScopeImport = vi.fn().mockResolvedValue({
      scope: { ...scope, allowedCidrs: ["192.0.2.7/32"], revision: 5 },
      scopeImport: { ...imported, status: "applied" },
    });
    const onApplied = vi.fn();
    const onClose = vi.fn();
    render(
      <ScopeImportDialog
        api={
          {
            createScopeImport,
            applyScopeImport,
            discardScopeImport: vi.fn(),
          } as unknown as ApiClient
        }
        engagementId="eng-1"
        scope={scope}
        providers={[provider]}
        onApplied={onApplied}
        onClose={onClose}
      />,
    );

    fireEvent.change(screen.getByLabelText("Choose scope document"), {
      target: {
        files: [new File(["target"], "scope.csv", { type: "text/csv" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Analyze document" }));

    await screen.findByText("192.0.2.7/32");
    expect(
      screen.getByRole("checkbox", { name: /192\.0\.2\.7\/32/ }),
    ).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: /admin\.example\.test/ }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Apply 1 target" }));

    await waitFor(() =>
      expect(applyScopeImport).toHaveBeenCalledWith(
        "eng-1",
        "import-1",
        ["allowed"],
        4,
      ),
    );
    expect(onApplied).toHaveBeenCalledWith(
      expect.objectContaining({ revision: 5 }),
    );
    expect(onClose).toHaveBeenCalled();
  });
});
