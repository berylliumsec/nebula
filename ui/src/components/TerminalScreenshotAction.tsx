import { Camera, X } from "lucide-react";
import { useState } from "react";
import { createPortal } from "react-dom";
import type {
  ContainerTerminalRuntimeSnapshot,
  ContainerTerminalSession,
  EvidenceSummary,
  EvidenceUploadRequest,
} from "../api/types";
import { ModalSurface } from "./DialogSystem";
import { ImageEditor, type ImageEditRecipe, type ImageEditorSaveResult } from "./imageEditor";
import {
  captureTerminalViewportPng,
  type TerminalPngCapture,
  type XtermViewportTerminal,
} from "./terminalCapture";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

const MAX_EVIDENCE_BYTES = 25 * 1024 * 1024;
const MAX_EDIT_RECIPE_BYTES = 16 * 1024;

interface PreservedTerminalCapture {
  capturedAt: string;
  capture: TerminalPngCapture;
  evidence: EvidenceSummary;
  sourceContext: Record<string, unknown>;
}

export interface TerminalScreenshotActionProps {
  capturedBy?: string;
  engagementId: string;
  getTerminal: () => XtermViewportTerminal | undefined;
  runtime: ContainerTerminalRuntimeSnapshot;
  session: ContainerTerminalSession;
  uploadEvidence: (request: EvidenceUploadRequest) => Promise<EvidenceSummary>;
}

function captureStamp(value: string): string {
  return value.replace(/[^0-9]/g, "").slice(0, 14);
}

async function encodeBlobBase64(blob: Blob): Promise<string> {
  if (blob.size > MAX_EVIDENCE_BYTES) {
    throw new Error("The screenshot exceeds the 25 MB evidence limit.");
  }
  let buffer: ArrayBuffer;
  try {
    if (typeof blob.arrayBuffer !== "function") throw new Error("Blob.arrayBuffer is unavailable.");
    buffer = await blob.arrayBuffer();
  } catch (reason) {
    if (typeof FileReader === "undefined") throw reason;
    buffer = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error ?? new Error("The screenshot could not be read for upload."));
      reader.onload = () => reader.result instanceof ArrayBuffer
        ? resolve(reader.result)
        : reject(new Error("The screenshot could not be read for upload."));
      reader.readAsArrayBuffer(blob);
    });
  }
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function boundedRecipe(recipe: ImageEditRecipe): Record<string, unknown> {
  const value: Record<string, unknown> = {
    version: recipe.version,
    source_width: recipe.sourceWidth,
    source_height: recipe.sourceHeight,
    output_width: recipe.outputWidth,
    output_height: recipe.outputHeight,
    operations: recipe.operations,
  };
  if (new TextEncoder().encode(JSON.stringify(value)).byteLength > MAX_EDIT_RECIPE_BYTES) {
    throw new Error("The edit recipe exceeds the 16 KiB evidence-manifest limit. Undo some edits before saving.");
  }
  return value;
}

export function TerminalScreenshotAction({
  capturedBy,
  engagementId,
  getTerminal,
  runtime,
  session,
  uploadEvidence,
}: TerminalScreenshotActionProps) {
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<PreservedTerminalCapture>();
  const [error, setError] = useState<string>();
  const [message, setMessage] = useState<string>();

  const capture = async () => {
    const terminal = getTerminal();
    if (busy) return;
    if (!terminal) {
      setMessage(undefined);
      setError("The terminal view is still initializing. Wait for it to connect, then try the screenshot again.");
      return;
    }
    setBusy(true);
    setError(undefined);
    setMessage(undefined);
    try {
      const capturedAt = new Date().toISOString();
      const png = await captureTerminalViewportPng(terminal);
      const sourceContext: Record<string, unknown> = {
        version: 1,
        source_kind: "terminal_viewport",
        project_id: engagementId,
        terminal_session_id: session.sessionId,
        runtime_image: runtime.image,
        runtime_image_digest: runtime.imageDigest,
        base_image_digest: runtime.baseImageDigest,
        captured_at: capturedAt,
        pixel_width: png.width,
        pixel_height: png.height,
        logical_width: png.logicalWidth,
        logical_height: png.logicalHeight,
        columns: png.snapshot.cols,
        rows: png.snapshot.rows,
        viewport_y: png.snapshot.viewportY,
        buffer_type: png.snapshot.bufferType,
      };
      const stamp = captureStamp(capturedAt);
      const evidence = await uploadEvidence({
        engagementId,
        filename: `terminal-screenshot-${stamp}.png`,
        title: `Terminal screenshot ${capturedAt}`,
        evidenceType: "terminal-screenshot",
        contentBase64: await encodeBlobBase64(png.blob),
        mediaType: "image/png",
        description: "Immutable capture of the visible Nebula terminal viewport.",
        source: "terminal-screenshot",
        capturedBy,
        sourceVersion: "terminal-viewport-v1",
        sourceContext,
        metadata: {
          terminal_session_id: session.sessionId,
          runtime_image_digest: runtime.imageDigest,
          captured_at: capturedAt,
          pixel_width: png.width,
          pixel_height: png.height,
        },
      });
      if (!evidence.artifactId) {
        throw new Error("The original screenshot was preserved, but Core did not return its artifact lineage identifier.");
      }
      setEditor({ capturedAt, capture: png, evidence, sourceContext });
      setMessage("Original terminal screenshot preserved as immutable evidence.");
    } catch (captureError) {
      void logCaughtDiagnostic("interface.terminal_screenshot_action.caught_failure_01", "A handled interface operation failed.", captureError, "terminal_screenshot_action");
      setError(captureError instanceof Error ? captureError.message : "The terminal screenshot could not be preserved.");
    } finally {
      setBusy(false);
    }
  };

  const saveDerived = async (result: ImageEditorSaveResult) => {
    if (!editor?.evidence.artifactId) throw new Error("The original evidence lineage is unavailable.");
    const derivedAt = new Date().toISOString();
    const recipe = boundedRecipe(result.recipe);
    const derived = await uploadEvidence({
      engagementId,
      filename: `terminal-screenshot-${captureStamp(editor.capturedAt)}-edited.png`,
      title: `Edited terminal screenshot ${editor.capturedAt}`,
      evidenceType: "terminal-screenshot",
      contentBase64: await encodeBlobBase64(result.blob),
      mediaType: "image/png",
      description: "Derived terminal screenshot with a versioned, reproducible edit manifest.",
      source: "terminal-screenshot-edit",
      capturedBy,
      sourceVersion: "terminal-image-edit-v1",
      parentArtifactId: editor.evidence.artifactId,
      sourceContext: {
        ...editor.sourceContext,
        parent_evidence_id: editor.evidence.id,
        parent_artifact_id: editor.evidence.artifactId,
        derived_at: derivedAt,
        output_pixel_width: result.width,
        output_pixel_height: result.height,
      },
      editRecipe: recipe,
      metadata: {
        parent_evidence_id: editor.evidence.id,
        parent_artifact_id: editor.evidence.artifactId,
        terminal_session_id: session.sessionId,
        runtime_image_digest: runtime.imageDigest,
        derived_at: derivedAt,
        pixel_width: result.width,
        pixel_height: result.height,
      },
    });
    setEditor(undefined);
    setError(undefined);
    setMessage(`Edited screenshot preserved as derived evidence ${derived.id.slice(0, 8)}.`);
  };

  return <>
    <button
      className="button secondary terminal-screenshot-button"
      type="button"
      disabled={busy}
      aria-busy={busy}
      title="Capture the visible terminal viewport as immutable evidence"
      onClick={() => void capture()}
    ><Camera size={15} /> {busy ? "Preserving…" : "Screenshot"}</button>
    {error && <DiagnosticErrorNotice error={error} fallback="The terminal capture could not be completed." compact />}
    {message && <span className="terminal-capture-feedback" role="status">{message}</span>}
    {editor && createPortal(<ModalSurface
      labelledBy="terminal-screenshot-editor-title"
      className="terminal-screenshot-dialog"
      onClose={() => setEditor(undefined)}
    >
      <header className="terminal-screenshot-dialog-header">
        <div><small>Original preserved · {editor.capture.width} × {editor.capture.height}px</small><h2 id="terminal-screenshot-editor-title">Edit terminal screenshot</h2><p>Saving creates a new evidence artifact linked to the unchanged original.</p></div>
        <button className="icon-button subtle" type="button" aria-label="Close image editor" onClick={() => setEditor(undefined)}><X size={17} /></button>
      </header>
      <div className="terminal-screenshot-dialog-body"><ImageEditor source={editor.capture.blob} onSave={saveDerived} onCancel={() => setEditor(undefined)} saveLabel="Save derived evidence" /></div>
    </ModalSurface>, document.body)}
  </>;
}
