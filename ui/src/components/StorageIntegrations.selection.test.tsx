import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

// A synthetic token in the shape Core issues for storage sources (#1096).
const SECRET = "nst_SYNTHETIC1096_9f8e7d6c5b4a39281706";
const WEBHOOK = "http://core.test/api/v1/storage/events";

const api = {
  baseUrl: "http://core.test/api/v1",
  storageDashboard: vi.fn(async () => ({ sources: [], dispatches: [], next_offset: null })),
  listHarnesses: vi.fn(async () => []),
  createStorageSource: vi.fn(async (body: Record<string, unknown>) => ({
    source: { id: "src-1", name: body.name, engagement_id: "project-1", enabled: false, revision: 1, reporting_interval_seconds: 900 },
    token: SECRET,
    webhook_url: WEBHOOK,
  })),
  createTemporaryChat: vi.fn(async () => ({ id: "tmp-1", backend: "provider", providerId: "provider-1", model: "model-1" })),
  keepTemporaryChatAlive: vi.fn(async () => undefined),
  discardTemporaryChat: vi.fn(async () => undefined),
  streamChat: vi.fn(async () => ({ message: { role: "assistant", content: "ok" } })),
  createHandoff: vi.fn(async () => ({ id: "handoff-1", revision: 1 })),
  cancelHandoff: vi.fn(async () => undefined),
};

const workspace = {
  api,
  engagement: { id: "project-1", name: "Project" },
  engagements: [{ id: "project-1", name: "Project" }],
  providers: [{ id: "provider-1", name: "Stub", enabled: true, models: ["model-1"], defaultModel: "model-1", effectiveDefaultModel: "model-1", kind: "openai", state: "healthy" }],
  harnesses: [],
  previewMode: false,
};

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));
vi.mock("../api/runtime", async (original) => ({ ...(await original<object>()), desktopDeviceId: async () => "device-1" }));

const { StorageIntegrations } = await import("./StorageIntegrations");
const { WorkbenchDraftProvider, useWorkbenchDrafts } = await import("../state/WorkbenchDraftContext");

function DraftProbe() {
  const { assistantDrafts, noteDraft } = useWorkbenchDrafts();
  return <output data-testid="drafts">{JSON.stringify({ assistant: assistantDrafts.map((draft) => draft.text), note: noteDraft?.text })}</output>;
}

async function issueToken() {
  render(<MemoryRouter initialEntries={["/settings#storage-integration-settings"]}><WorkbenchDraftProvider>
    <StorageIntegrations /><DraftProbe />
  </WorkbenchDraftProvider></MemoryRouter>);
  await userEvent.type(await screen.findByLabelText("Source name"), "NAS");
  await waitFor(() => expect(screen.getByRole("button", { name: /Add disabled source/ })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: /Add disabled source/ }));
  const token = await screen.findByLabelText("Source token") as HTMLInputElement;
  expect(token.value).toBe(SECRET);
  return token;
}

describe("storage source token and selection actions", () => {
  it("offers no selection actions for the issued token, so it cannot reach Ask Nebula or a draft", async () => {
    const token = await issueToken();
    // The field selects itself on focus; the browser then fires select.
    await userEvent.click(token);
    fireEvent.select(token);
    expect(token.selectionStart).toBe(0);
    expect(token.selectionEnd).toBe(SECRET.length);
    expect(screen.queryByRole("toolbar", { name: "Selected text actions" })).toBeNull();
    fireEvent.keyUp(token, { key: "c", ctrlKey: true });
    expect(screen.queryByRole("toolbar", { name: "Selected text actions" })).toBeNull();
    expect(screen.getByTestId("drafts").textContent).not.toContain(SECRET);
    expect(api.createTemporaryChat).not.toHaveBeenCalled();
    expect(api.streamChat).not.toHaveBeenCalled();
    expect(JSON.stringify(api.createHandoff.mock.calls)).not.toContain(SECRET);
  });

  it("still offers selection actions for the webhook URL beside it", async () => {
    await issueToken();
    const webhook = screen.getByLabelText("Webhook URL") as HTMLInputElement;
    await userEvent.click(webhook);
    fireEvent.select(webhook);
    const toolbar = await screen.findByRole("toolbar", { name: "Selected text actions" });
    expect(toolbar.textContent).toMatch(/Ask Nebula/);
    await userEvent.click(screen.getByRole("button", { name: /Add context to chat/ }));
    await waitFor(() => expect(screen.getByTestId("drafts").textContent).toContain(WEBHOOK));
  });
});
