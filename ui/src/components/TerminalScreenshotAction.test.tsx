import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ContainerTerminalRuntimeSnapshot, ContainerTerminalSession, EvidenceSummary } from "../api/types";
import { TerminalScreenshotAction } from "./TerminalScreenshotAction";
import type { XtermViewportTerminal } from "./terminalCapture";

const captureSpies = vi.hoisted(() => ({ capture: vi.fn() }));

vi.mock("./terminalCapture", () => ({
  captureTerminalViewportPng: captureSpies.capture,
}));

vi.mock("./imageEditor", () => ({
  ImageEditor: ({ onSave, onCancel }: {
    onSave: (result: unknown) => Promise<void>;
    onCancel: () => void;
  }) => <div aria-label="Mock image editor">
    <button type="button" onClick={() => void onSave({
      blob: bytesBlob("derived", "image/png"),
      width: 320,
      height: 160,
      recipe: {
        version: 1,
        sourceWidth: 640,
        sourceHeight: 320,
        outputWidth: 320,
        outputHeight: 160,
        operations: [{ id: "crop-1", type: "crop", rect: { x: 0, y: 0, width: 320, height: 160 } }],
      },
    })}>Save derived evidence</button>
    <button type="button" onClick={onCancel}>Cancel</button>
  </div>,
}));

function bytesBlob(value: string, type: string): Blob {
  const bytes = new TextEncoder().encode(value);
  const blob = new Blob([bytes], { type });
  Object.defineProperty(blob, "arrayBuffer", { value: async () => bytes.buffer });
  return blob;
}

function evidence(id: string, artifactId: string): EvidenceSummary {
  return {
    id,
    engagementId: "project-1",
    evidenceType: "terminal-screenshot",
    title: id,
    description: "",
    artifactId,
    assetIds: [],
    capturedAt: "2026-07-13T20:00:00Z",
    createdAt: "2026-07-13T20:00:00Z",
    updatedAt: "2026-07-13T20:00:00Z",
    metadata: {},
  };
}

describe("TerminalScreenshotAction", () => {
  beforeEach(() => captureSpies.capture.mockReset());

  it("explains when the terminal renderer is not ready instead of ignoring the action", async () => {
    const uploadEvidence = vi.fn();
    const user = userEvent.setup();

    render(<TerminalScreenshotAction
      engagementId="project-1"
      getTerminal={() => undefined}
      runtime={{} as ContainerTerminalRuntimeSnapshot}
      session={{ sessionId: "terminal-1" } as ContainerTerminalSession}
      uploadEvidence={uploadEvidence}
    />);

    await user.click(screen.getByRole("button", { name: "Screenshot" }));
    expect(screen.getByText(/terminal view is still initializing/i)).toBeVisible();
    expect(captureSpies.capture).not.toHaveBeenCalled();
    expect(uploadEvidence).not.toHaveBeenCalled();
  });

  it("uploads captures in WebViews that require FileReader for Blob bytes", async () => {
    const legacyBlob = new Blob([new TextEncoder().encode("original")], { type: "image/png" });
    Object.defineProperty(legacyBlob, "arrayBuffer", { value: undefined });
    captureSpies.capture.mockResolvedValue({
      blob: legacyBlob,
      width: 640,
      height: 320,
      logicalWidth: 640,
      logicalHeight: 320,
      snapshot: { cols: 80, rows: 24, viewportY: 0, bufferType: "normal" },
    });
    const uploadEvidence = vi.fn().mockResolvedValue(evidence("evidence-original", "artifact-original"));
    const user = userEvent.setup();

    render(<TerminalScreenshotAction
      engagementId="project-1"
      getTerminal={() => ({ cols: 80, rows: 24 }) as XtermViewportTerminal}
      runtime={{ image: "image", imageDigest: "digest", baseImageDigest: "base" } as ContainerTerminalRuntimeSnapshot}
      session={{ sessionId: "terminal-1" } as ContainerTerminalSession}
      uploadEvidence={uploadEvidence}
    />);

    await user.click(screen.getByRole("button", { name: "Screenshot" }));
    await waitFor(() => expect(uploadEvidence).toHaveBeenCalled());
    expect(uploadEvidence.mock.calls[0][0].contentBase64).toBe(btoa("original"));
  });

  it("preserves the original viewport before saving a separately linked derived artifact", async () => {
    const terminal = { cols: 80, rows: 24 } as XtermViewportTerminal;
    captureSpies.capture.mockResolvedValue({
      blob: bytesBlob("original", "image/png"),
      width: 640,
      height: 320,
      logicalWidth: 640,
      logicalHeight: 320,
      snapshot: { cols: 80, rows: 24, viewportY: 12, bufferType: "alternate" },
    });
    const uploadEvidence = vi.fn()
      .mockResolvedValueOnce(evidence("evidence-original", "artifact-original"))
      .mockResolvedValueOnce(evidence("evidence-derived", "artifact-derived"));
    const runtime = {
      image: `sha256:${"c".repeat(64)}`,
      imageDigest: `sha256:${"c".repeat(64)}`,
      baseImage: `docker.io/kalilinux/kali-rolling@sha256:${"b".repeat(64)}`,
      baseImageDigest: `sha256:${"b".repeat(64)}`,
    } as ContainerTerminalRuntimeSnapshot;
    const session = { sessionId: "terminal-1" } as ContainerTerminalSession;
    const user = userEvent.setup();

    render(<TerminalScreenshotAction
      capturedBy="operator-1"
      engagementId="project-1"
      getTerminal={() => terminal}
      runtime={runtime}
      session={session}
      uploadEvidence={uploadEvidence}
    />);

    await user.click(screen.getByRole("button", { name: "Screenshot" }));
    expect(await screen.findByRole("heading", { name: "Edit terminal screenshot" })).toBeVisible();
    expect(captureSpies.capture).toHaveBeenCalledWith(terminal);
    expect(uploadEvidence).toHaveBeenCalledTimes(1);
    expect(uploadEvidence.mock.calls[0][0]).toMatchObject({
      engagementId: "project-1",
      evidenceType: "terminal-screenshot",
      mediaType: "image/png",
      source: "terminal-screenshot",
      capturedBy: "operator-1",
      sourceContext: {
        version: 1,
        project_id: "project-1",
        terminal_session_id: "terminal-1",
        runtime_image_digest: runtime.imageDigest,
        pixel_width: 640,
        pixel_height: 320,
        columns: 80,
        rows: 24,
        viewport_y: 12,
        buffer_type: "alternate",
      },
    });
    expect(uploadEvidence.mock.calls[0][0].parentArtifactId).toBeUndefined();

    await user.click(screen.getByRole("button", { name: "Save derived evidence" }));
    await waitFor(() => expect(uploadEvidence).toHaveBeenCalledTimes(2));
    expect(uploadEvidence.mock.calls[1][0]).toMatchObject({
      source: "terminal-screenshot-edit",
      parentArtifactId: "artifact-original",
      sourceContext: {
        parent_evidence_id: "evidence-original",
        parent_artifact_id: "artifact-original",
        terminal_session_id: "terminal-1",
        output_pixel_width: 320,
        output_pixel_height: 160,
      },
      editRecipe: {
        version: 1,
        source_width: 640,
        source_height: 320,
        output_width: 320,
        output_height: 160,
        operations: [{ id: "crop-1", type: "crop" }],
      },
    });
    expect(screen.queryByRole("heading", { name: "Edit terminal screenshot" })).not.toBeInTheDocument();
    expect(screen.getByText(/Edited screenshot preserved as derived evidence/)).toBeVisible();
  });
});
