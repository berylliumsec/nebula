import "fake-indexeddb/auto";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkbenchEditorProvider, useAdoptWorkbenchEditorSession, useWorkbenchEditor } from "./WorkbenchEditorContext";
import { clearEditorSessions, loadEditorSessions } from "./editorSessionPersistence";

vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

afterEach(async () => { cleanup(); await clearEditorSessions(); });

describe("editor hot-exit provider", () => {
  it("keeps buffers separate per chat and adopts a new chat draft", async () => {
    const editor = renderHook(() => ({
      first: useWorkbenchEditor("chat-a"),
      second: useWorkbenchEditor("chat-b"),
      draft: useWorkbenchEditor("draft-new"),
      saved: useWorkbenchEditor("chat-new"),
      adopt: useAdoptWorkbenchEditorSession(),
    }), { wrapper: WorkbenchEditorProvider });
    await waitFor(() => expect(editor.result.current.first.persistenceState).toBe("ready"));
    act(() => editor.result.current.first.setBuffer({ id: "a", content: "A draft", savedContent: "", existing: false, filePath: "shared.txt" }));
    expect(editor.result.current.second.buffers).toHaveLength(0);
    act(() => editor.result.current.draft.setBuffer({ id: "new", content: "Before first message", savedContent: "", existing: false, filePath: "new.txt" }));
    act(() => editor.result.current.adopt("draft-new", "chat-new"));
    expect(editor.result.current.saved.buffer?.content).toBe("Before first message");
    expect(editor.result.current.draft.buffers).toHaveLength(0);
    expect(editor.result.current.first.buffer?.content).toBe("A draft");
  });
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
