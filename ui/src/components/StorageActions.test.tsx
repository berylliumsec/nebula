import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import type { StorageAction, StorageDashboard } from "../api/types";
import { StorageChatActions } from "./StorageActions";

const offered: StorageAction = {
  id: "action-1", source_id: "source-1", incident_id: "incident-1", title: "Archive copy · /fixture/artifacts",
  status: "offered", digest: null, effects: [], expires_at: null, receipt: null, error: null,
};

function dashboard(actions: StorageAction[]): StorageDashboard {
  return { sources: [], devices: [], volumes: [], dispatches: [],
    incidents: [{ id: "incident-1", source_id: "source-1", volume_key: "volume-1", state: "warning", opened_at: "2026-10-14T00:00:00Z", resolved_at: null,
      chat_session_id: "chat-1", dispatch_status: "queued", dispatch_error: null, callback_error: null }], actions };
}

describe("StorageChatActions", () => {
  it("keeps offered and completed actions behind a keyboard-accessible disclosure", async () => {
    const user = userEvent.setup();
    const api = { storageDashboard: vi.fn().mockResolvedValue(dashboard([offered, { ...offered, id: "action-2", status: "complete", title: "Completed copy" }])) } as unknown as ApiClient;
    render(<StorageChatActions api={api} sessionId="chat-1" />);

    const toggle = await screen.findByRole("button", { name: /Storage incident.*2 source actions/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("region", { name: "Storage actions" })).toBeNull();
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("region", { name: "Storage actions" })).toBeVisible();
    expect(screen.getByText("Completed copy")).toBeVisible();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("region", { name: "Storage actions" })).toBeNull();
  });

  it("keeps a failed preview visible in the compact strip without exposing a long server error", async () => {
    const user = userEvent.setup();
    const api = { storageDashboard: vi.fn().mockResolvedValue(dashboard([offered])),
      storageAction: vi.fn().mockRejectedValue(new ApiError("Server error at http://127.0.0.1/source/preview with a long diagnostic", 500)) } as unknown as ApiClient;
    render(<StorageChatActions api={api} sessionId="chat-1" />);

    const toggle = await screen.findByRole("button", { name: /Storage incident.*1 source action/ });
    await user.click(toggle);
    await user.click(within(screen.getByRole("region", { name: "Storage actions" })).getByRole("button", { name: "Preview effects" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Preview failed. Chat remains available"));
    expect(screen.getByText("Error details")).toBeVisible();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("alert")).toHaveTextContent("Preview failed. Chat remains available");
    expect(screen.getByText(/Server error at http/)).not.toBeVisible();
  });
});
