import "fake-indexeddb/auto";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkbenchEditorProvider, useWorkbenchEditor } from "./WorkbenchEditorContext";
import { clearEditorSessions, loadEditorSessions } from "./editorSessionPersistence";

vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

afterEach(async () => { cleanup(); await clearEditorSessions(); });

describe("editor hot-exit provider", () => {
  it("recovers the active 21st draft after remount", async () => {
    const editor = renderHook(() => useWorkbenchEditor("project"), { wrapper: WorkbenchEditorProvider });
    await waitFor(() => expect(editor.result.current.persistenceState).toBe("ready"));
    act(() => {
      for (let index = 0; index < 21; index++) {
        editor.result.current.setBuffer({ id: `draft-${index}`, content: `text ${index}`, savedContent: "", existing: false, filePath: `${index}.txt` });
      }
    });
    await waitFor(async () => expect((await loadEditorSessions()).project?.buffers).toHaveLength(21));
    editor.unmount();
    const restored = renderHook(() => useWorkbenchEditor("project"), { wrapper: WorkbenchEditorProvider });
    await waitFor(() => expect(restored.result.current.buffer?.content).toBe("text 20"));
    expect(restored.result.current.buffers).toHaveLength(21);
  });

  it("keeps oversized edits live, surfaces recovery failure, and recovers after retry", async () => {
    const editor = renderHook(() => useWorkbenchEditor("project"), { wrapper: WorkbenchEditorProvider });
    await waitFor(() => expect(editor.result.current.persistenceState).toBe("ready"));
    act(() => editor.result.current.setBuffer({ id: "first", content: "recoverable", savedContent: "", existing: false, filePath: "first.txt" }));
    await waitFor(async () => expect((await loadEditorSessions()).project?.buffers[0].content).toBe("recoverable"));
    act(() => {
      for (let index = 0; index < 9; index++) {
        editor.result.current.setBuffer({ id: `large-${index}`, content: "x".repeat(1024 * 1024), savedContent: "", existing: false, filePath: `${index}.txt` });
      }
    });
    await waitFor(() => expect(editor.result.current.persistenceState).toBe("failed"));
    expect(editor.result.current.persistenceError).toContain("8 MiB");
    expect(editor.result.current.buffers).toHaveLength(10);
    expect((await loadEditorSessions()).project.buffers).toHaveLength(1);
    act(() => editor.result.current.updateBuffers((buffers) => buffers.filter((buffer) => buffer.id === "first")));
    act(() => editor.result.current.retryPersistence());
    await waitFor(() => expect(editor.result.current.persistenceState).toBe("ready"));
    expect(editor.result.current.persistenceError).toBeUndefined();
    expect((await loadEditorSessions()).project.buffers[0].content).toBe("recoverable");
  });
});
