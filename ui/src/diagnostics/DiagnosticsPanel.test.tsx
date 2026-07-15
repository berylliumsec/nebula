import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DiagnosticsPanel } from "./DiagnosticsPanel";

const mocks = vi.hoisted(() => ({
  confirm: vi.fn(),
  log: vi.fn(),
  reveal: vi.fn(),
  updateNative: vi.fn(),
  nativeSettings: vi.fn(),
  nativeStatus: vi.fn(),
  nativeFiles: vi.fn(),
  nativeErrors: vi.fn(),
  api: {
    diagnosticsSettings: vi.fn(),
    diagnosticsFiles: vi.fn(),
    diagnosticErrors: vi.fn(),
    updateDiagnosticsSettings: vi.fn(),
    exportDiagnostics: vi.fn(),
  },
  workspace: {} as { api?: unknown; workspaceState: string },
}));

vi.mock("../components/DialogSystem", () => ({
  useConfirmation: () => mocks.confirm,
}));

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => mocks.workspace,
}));

vi.mock("./logger", () => ({
  diagnosticsFallbackErrors: () => [],
  isDiagnosticsAvailable: () => true,
  logDiagnostic: mocks.log,
  nativeDiagnosticFiles: mocks.nativeFiles,
  nativeDiagnosticSettings: mocks.nativeSettings,
  nativeDiagnosticStatus: mocks.nativeStatus,
  nativeRecentErrors: mocks.nativeErrors,
  normalizeDiagnosticSettings: (value: unknown) => {
    const candidate = value && typeof value === "object" ? value as Record<string, unknown> : {};
    return {
      schema: "nebula.diagnostics-settings/v1",
      global_level: candidate.global_level ?? "error",
      feature_levels: candidate.feature_levels ?? {},
    };
  },
  revealNativeLogs: mocks.reveal,
  setDiagnosticSettings: vi.fn(),
  updateNativeDiagnosticSettings: mocks.updateNative,
}));

const settings = {
  schema: "nebula.diagnostics-settings/v1" as const,
  global_level: "error" as const,
  feature_levels: {},
};

const status = {
  schema: "nebula.diagnostics-status/v1" as const,
  writable: true,
  degraded: false,
  global_level: "error" as const,
  feature_levels: {},
  disk_usage_bytes: 1024,
  dropped_record_count: 0,
};

const errorRecord = {
  schema: "nebula.diagnostic/v1" as const,
  level: "ERROR" as const,
  feature: "chat" as const,
  event_code: "chat.stream.failed",
  message: "A chat stream could not complete.",
  error_id: "err_visible_123",
  request_id: "req_visible_123",
  stage: "stream",
  retryable: true,
};

describe("DiagnosticsPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    mocks.workspace.api = mocks.api;
    mocks.workspace.workspaceState = "ready";
    mocks.api.diagnosticsSettings.mockResolvedValue(settings);
    mocks.api.diagnosticsFiles.mockResolvedValue({
      files: [{ name: "chat.log", size_bytes: 1024, modified_at: "2026-07-14T12:00:00Z" }],
      health: status,
    });
    mocks.api.diagnosticErrors.mockResolvedValue([errorRecord]);
    mocks.api.updateDiagnosticsSettings.mockImplementation(async (value: unknown) => value);
    mocks.api.exportDiagnostics.mockResolvedValue(new Blob(["zip"]));
    mocks.confirm.mockResolvedValue(true);
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:test") });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  });

  it("loads errors, filters them, applies global and feature levels, and confirms export", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsPanel />);

    expect(await screen.findByText("A chat stream could not complete.")).toBeVisible();
    await user.click(screen.getByText("A chat stream could not complete."));
    expect(screen.getByText("err_visible_123")).toBeVisible();
    expect(screen.getByText("chat.log")).toBeVisible();

    await user.selectOptions(screen.getByLabelText("Global level"), "info");
    await user.click(screen.getByText(/Per-feature overrides/));
    await user.selectOptions(screen.getByLabelText("chat"), "debug");
    await user.click(screen.getByRole("button", { name: "Save logging levels" }));
    await waitFor(() => expect(mocks.api.updateDiagnosticsSettings).toHaveBeenCalledWith({
      ...settings,
      global_level: "info",
      feature_levels: { chat: "debug" },
    }));

    await user.selectOptions(screen.getByLabelText("Filter diagnostic errors by feature"), "chat");
    await waitFor(() => expect(mocks.api.diagnosticErrors).toHaveBeenCalledWith("chat"));
    await user.click(screen.getByRole("button", { name: "Export sanitized ZIP" }));
    expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({
      title: "Export local diagnostics?",
      confirmLabel: "Export diagnostics",
    }));
    await waitFor(() => expect(mocks.api.exportDiagnostics).toHaveBeenCalledOnce());
  });

  it("keeps native errors and the fixed open-folder action available without Core", async () => {
    const user = userEvent.setup();
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    mocks.workspace.api = undefined;
    mocks.workspace.workspaceState = "failed";
    mocks.nativeSettings.mockResolvedValue(settings);
    mocks.nativeStatus.mockResolvedValue(status);
    mocks.nativeFiles.mockResolvedValue([
      { name: "desktop.log", size_bytes: 12, modified_at: "2026-07-14T12:00:00Z" },
    ]);
    mocks.nativeErrors.mockResolvedValue([{ ...errorRecord, feature: "desktop" }]);
    mocks.reveal.mockResolvedValue(undefined);

    render(<DiagnosticsPanel />);

    expect(await screen.findByText("A chat stream could not complete.")).toBeVisible();
    expect(screen.getByText("desktop.log")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Open logs folder" }));
    expect(mocks.reveal).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Export sanitized ZIP" })).toBeDisabled();
  });

  it("merges degraded Core logger health into the native viewer", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    mocks.nativeSettings.mockResolvedValue(settings);
    mocks.nativeStatus.mockResolvedValue(status);
    mocks.nativeFiles.mockResolvedValue([]);
    mocks.nativeErrors.mockResolvedValue([]);
    mocks.api.diagnosticsFiles.mockResolvedValue({
      files: [],
      health: {
        ...status,
        writable: false,
        degraded: true,
        dropped_record_count: 3,
        last_failure: { message: "Core diagnostic storage is unavailable." },
      },
    });

    render(<DiagnosticsPanel />);

    expect(await screen.findByText("Diagnostics are degraded")).toBeVisible();
    expect(screen.getByText("Core diagnostic storage is unavailable.")).toBeVisible();
    expect(screen.getByText("3")).toBeVisible();
  });
});
