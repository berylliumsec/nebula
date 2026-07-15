import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DiagnosticsPanel } from "./DiagnosticsPanel";

const mocks = vi.hoisted(() => ({
  confirm: vi.fn(),
  log: vi.fn(),
  logCaught: vi.fn(),
  reveal: vi.fn(),
  updateNative: vi.fn(),
  nativeSettings: vi.fn(),
  nativeStatus: vi.fn(),
  nativeFiles: vi.fn(),
  nativeErrors: vi.fn(),
  clipboard: vi.fn(),
  api: {
    diagnosticsSettings: vi.fn(),
    diagnosticsFiles: vi.fn(),
    diagnosticErrors: vi.fn(),
    updateDiagnosticsSettings: vi.fn(),
    exportDiagnostics: vi.fn(),
    health: vi.fn(),
    setupStatus: vi.fn(),
  },
  workspace: {} as {
    api?: unknown;
    coreError?: string;
    health?: unknown;
    setupStatus?: unknown;
    workspaceState: string;
  },
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
  logCaughtDiagnostic: mocks.logCaught,
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

const health = {
  status: "ok" as const,
  version: "3.0.0",
  mode: "local" as const,
  runner: "ready" as const,
  containerTerminal: "configured" as const,
};

const setup = {
  core: { status: "ready" as const, detail: "Core is ready." },
  terminal: {
    status: "ready" as const,
    candidates: [],
    imagePreparation: {
      phase: "ready" as const,
      progressIndeterminate: false,
      canCancel: false,
      canRetry: false,
    },
    detail: "Podman is ready.",
  },
  assistant: { status: "configured" as const },
};

const errorRecord = {
  schema: "nebula.diagnostic/v1" as const,
  timestamp: "2026-07-14T12:00:00Z",
  level: "ERROR" as const,
  feature: "chat" as const,
  event_code: "chat.stream.failed",
  message: "A chat stream could not complete.",
  safe_failure_cause: "The configured model provider stopped the stream.",
  error_id: "err_visible_123",
  request_id: "req_visible_123",
  operation_id: "op_visible_123",
  stage: "stream",
  outcome: "failure",
  retryable: true,
  exception_type: "ProviderError",
  exception_chain: ["ProviderError", "TimeoutError"],
  stack_frames: [{ module: "chat", function: "stream", line: 42 }],
  metadata: { provider: "local", component: "response_stream" },
};

describe("DiagnosticsPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/settings#diagnostics-settings");
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    mocks.workspace.api = mocks.api;
    mocks.workspace.coreError = undefined;
    mocks.workspace.health = health;
    mocks.workspace.setupStatus = setup;
    mocks.workspace.workspaceState = "ready";
    mocks.api.diagnosticsSettings.mockResolvedValue(settings);
    mocks.api.diagnosticsFiles.mockResolvedValue({
      files: [{ name: "chat.log", size_bytes: 1024, modified_at: "2026-07-14T12:00:00Z" }],
      health: status,
    });
    mocks.api.diagnosticErrors.mockResolvedValue([errorRecord]);
    mocks.api.health.mockResolvedValue(health);
    mocks.api.setupStatus.mockResolvedValue(setup);
    mocks.api.updateDiagnosticsSettings.mockImplementation(async (value: unknown) => value);
    mocks.api.exportDiagnostics.mockResolvedValue(new Blob(["zip"]));
    mocks.confirm.mockResolvedValue(true);
    mocks.clipboard.mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: mocks.clipboard } });
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:test") });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  });

  it("leads with live status and explains failures before technical identifiers", async () => {
    const user = userEvent.setup();
    const clipboard = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    render(<DiagnosticsPanel />);

    expect(await screen.findByText("Core is responding")).toBeVisible();
    expect(screen.getByText("Terminal runtime is ready")).toBeVisible();
    expect(screen.getByText("Local logging is healthy")).toBeVisible();
    expect(screen.getByText("A chat stream could not complete.")).toBeVisible();
    expect(screen.getByText("Assistant chat · Response stream")).toBeVisible();
    expect(screen.getByText("The configured model provider stopped the stream.")).toBeVisible();
    expect(screen.getByText("Retry the original action.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Assistant" })).toHaveAttribute("href", "/?view=chat");

    expect(screen.getByText("err_visible_123")).not.toBeVisible();
    await user.click(screen.getByText("Technical details"));
    expect(screen.getByText("err_visible_123")).toBeVisible();
    expect(screen.getByText("ProviderError → TimeoutError")).toBeVisible();
    expect(screen.getByText("chat.stream:42")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Copy technical details" }));
    expect(clipboard).toHaveBeenCalledWith(expect.stringContaining('"event_code": "chat.stream.failed"'));
  });

  it("keeps logging controls and support files in the advanced section", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsPanel />);
    await screen.findByText("A chat stream could not complete.");

    expect(screen.getByText("chat.log")).not.toBeVisible();
    await user.click(screen.getByText("Advanced diagnostics and logging"));
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

  it("keeps available failures visible when an independent source fails", async () => {
    mocks.api.diagnosticsSettings.mockRejectedValue(new Error("settings unavailable"));
    render(<DiagnosticsPanel />);

    expect(await screen.findByText("A chat stream could not complete.")).toBeVisible();
    expect(screen.getByText("Some diagnostic details are unavailable.")).toBeVisible();
    expect(screen.getByText(/logging preferences/)).toBeVisible();
    expect(mocks.log).toHaveBeenCalledWith(expect.objectContaining({
      eventCode: "interface.diagnostics.viewer_load_failed",
      outcome: "degraded",
    }));
  });

  it("keeps native failures and fixed folder access available without Core", async () => {
    const user = userEvent.setup();
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    mocks.workspace.api = undefined;
    mocks.workspace.workspaceState = "failed";
    mocks.workspace.coreError = "The Core sidecar stopped.";
    mocks.nativeSettings.mockResolvedValue(settings);
    mocks.nativeStatus.mockResolvedValue(status);
    mocks.nativeFiles.mockResolvedValue([
      { name: "desktop.log", size_bytes: 12, modified_at: "2026-07-14T12:00:00Z" },
    ]);
    mocks.nativeErrors.mockResolvedValue([{ ...errorRecord, feature: "desktop" }]);
    mocks.reveal.mockResolvedValue(undefined);

    render(<DiagnosticsPanel />);

    expect(await screen.findByText("Core is unavailable")).toBeVisible();
    expect(screen.getByText("The Core sidecar stopped.")).toBeVisible();
    expect(screen.getByText("A chat stream could not complete.")).toBeVisible();
    await user.click(screen.getByText("Advanced diagnostics and logging"));
    expect(screen.getByText("desktop.log")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Open logs folder" }));
    expect(mocks.reveal).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Export sanitized ZIP" })).toBeDisabled();
  });

  it("shows runtime and logger degradation as active status", async () => {
    (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    mocks.nativeSettings.mockResolvedValue(settings);
    mocks.nativeStatus.mockResolvedValue(status);
    mocks.nativeFiles.mockResolvedValue([]);
    mocks.nativeErrors.mockResolvedValue([]);
    mocks.api.setupStatus.mockResolvedValue({
      ...setup,
      terminal: { ...setup.terminal, status: "needs_runner", detail: "No supported rootless runner is ready." },
    });
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

    expect(await screen.findByText("Terminal runtime needs attention")).toBeVisible();
    expect(screen.getByText("No supported rootless runner is ready.")).toBeVisible();
    expect(screen.getByText("Local logging needs attention")).toBeVisible();
    expect(screen.getByText("Core diagnostic storage is unavailable.")).toBeVisible();
  });

  it("opens and focuses a requested failure reference", async () => {
    window.history.replaceState(null, "", "/settings?diagnostic=req_visible_123#diagnostics-settings");
    render(<DiagnosticsPanel />);

    expect(await screen.findByText(/Showing requested failure/)).toBeVisible();
    expect(screen.getByText("req_visible_123", { selector: ".diagnostic-target-notice code" })).toBeVisible();
    expect(screen.getByText("err_visible_123")).toBeVisible();
    await waitFor(() => expect(document.activeElement).toHaveClass("diagnostic-failure-card", "targeted"));
  });

  it("reports an expired deep link without inventing recovery guidance", async () => {
    window.history.replaceState(null, "", "/settings?diagnostic=err_expired_123#diagnostics-settings");
    mocks.api.diagnosticErrors.mockResolvedValue([{ ...errorRecord, retryable: undefined, safe_failure_cause: undefined }]);
    render(<DiagnosticsPanel />);

    expect(await screen.findByText(/is no longer in recent diagnostics/)).toBeVisible();
    expect(screen.getByText("No additional safe cause was recorded.")).toBeVisible();
    expect(screen.getByText("No verified recovery procedure is available.")).toBeVisible();
  });
});
