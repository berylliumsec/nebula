import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ButlerIssues } from "./ButlerIssues";

function client(request: ReturnType<typeof vi.fn>): ApiClient {
  return {
    request,
    listHarnesses: vi.fn().mockResolvedValue([{
      id: "codex-1", name: "Host Codex", kind: "codex_app_server", enabled: true, defaultModel: "codex-test",
    }]),
    listMcpServers: vi.fn().mockResolvedValue([]),
    credentialVaultStatus: vi.fn().mockResolvedValue({ available: true, state: "available" }),
    createCredential: vi.fn().mockResolvedValue({ reference: "vault:credential" }),
  } as unknown as ApiClient;
}

describe("ButlerIssues", () => {
  it("shows held drafts and published receipts with a local revision action", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockResolvedValue({
      destinations: [{ id: "destination", revision: 1, name: "Nebula", repository: "owner/nebula",
        publisher: "github", credential_ref: "vault:credential", review_profile_id: "codex-1",
        auto_publish: true, enabled: true }],
      candidates: [
        { id: "held", revision: 1, destination_id: "destination", title: "Refresh loses selection",
          body: "The saved selection is lost on refresh.", reproduction: "Select a saved item and refresh.",
          confidence: "hypothesis", public_independent: false, status: "held",
          reason: "public_reproduction_not_independent", source_kind: "chat_turn" },
        { id: "posted", revision: 1, destination_id: "destination", title: "Export fails",
          body: "Export fails for saved items.", reproduction: "Create and export an item.",
          confidence: "observed", public_independent: true, status: "published", reason: "",
          issue_url: "https://github.com/owner/nebula/issues/11", source_kind: "incident" },
      ], held_scans: [],
    });
    render(<ButlerIssues api={client(request)} projectId="project-a" online />);
    const held = await screen.findByRole("list", { name: "Held improvement drafts" });
    expect(within(held).getByText("public reproduction not independent")).toBeVisible();
    await user.click(within(held).getByRole("button", { name: "Revise draft" }));
    expect(screen.getByLabelText("Issue body")).toHaveValue("The saved selection is lost on refresh.");
    expect(screen.getByRole("link", { name: "Export fails" })).toHaveAttribute("href", "https://github.com/owner/nebula/issues/11");
  });

  it("discovers a repository before granting automatic publication", async () => {
    const user = userEvent.setup();
    const request = vi.fn().mockImplementation(async (path: string, options?: { method?: string }) => {
      if (path.includes("github-repositories")) return { repositories: [{ repository: "owner/nebula", visibility: "public" }], has_more: false };
      if (options?.method === "POST") return { id: "saved" };
      return { destinations: [], candidates: [], held_scans: [] };
    });
    const api = client(request);
    render(<ButlerIssues api={api} projectId="project-a" online />);
    await user.click(await screen.findByRole("button", { name: "Add issue destination" }));
    await user.type(screen.getByLabelText("Name"), "Nebula bugs");
    await user.selectOptions(screen.getByLabelText("Host Codex reviewer"), "codex-1");
    await user.type(screen.getByLabelText("GitHub token"), "test-secret");
    await user.click(screen.getByRole("button", { name: "Save token and browse repositories" }));
    await user.selectOptions(await screen.findByLabelText("Repository"), "owner/nebula");
    await user.click(screen.getByLabelText(/Automatically publish drafts/));
    await user.click(screen.getByRole("button", { name: "Save destination" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith(
      "engagements/project-a/butler/issues/destinations",
      expect.objectContaining({ method: "POST", body: expect.stringContaining('"auto_publish":true') }),
    ));
  });
});
