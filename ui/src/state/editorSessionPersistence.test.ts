import "fake-indexeddb/auto";
import { afterEach, describe, expect, it } from "vitest";
import { clearEditorSessions, loadEditorSessions, normalizeEditorSessions, saveEditorSessions } from "./editorSessionPersistence";

afterEach(() => clearEditorSessions());

describe("editorSessionPersistence", () => {
  it("restores exact dirty drafts and compacts clean workspace files to identities", async () => {
    await saveEditorSessions({
      project: {
        activeId: "dirty",
        primaryId: "dirty",
        secondaryId: "clean",
        buffers: [
          { id: "dirty", content: "λ changed\n", expectedSha256: "a".repeat(64), existing: true, filePath: "dirty.py", savedContent: "λ base\n" },
          { id: "clean", content: "saved bytes\n", expectedSha256: "b".repeat(64), existing: true, filePath: "clean.py", savedContent: "saved bytes\n" },
          { id: "draft", content: "unsaved", existing: false, filePath: "draft.txt", savedContent: "" },
        ],
      },
    });

    await expect(loadEditorSessions()).resolves.toEqual({
      project: {
        activeId: "dirty",
        primaryId: "dirty",
        secondaryId: "clean",
        buffers: [
          { id: "dirty", content: "λ changed\n", expectedSha256: "a".repeat(64), existing: true, filePath: "dirty.py", restoreFromCore: false, savedContent: "λ base\n" },
          { id: "clean", content: "", expectedSha256: "b".repeat(64), existing: true, filePath: "clean.py", restoreFromCore: true, savedContent: "" },
          { id: "draft", content: "unsaved", existing: false, filePath: "draft.txt", restoreFromCore: false, savedContent: "" },
        ],
      },
    });
  });

  it("fails closed on malformed, oversized, and dangling session state", () => {
    expect(normalizeEditorSessions({ schema: "wrong", sessions: {} })).toEqual({});
    expect(normalizeEditorSessions({
      schema: "nebula.editor-sessions/v1",
      sessions: {
        project: {
          activeId: "missing",
          primaryId: "missing",
          secondaryId: "missing",
          buffers: [
            { id: "bad", content: "x", expectedSha256: "not-a-hash", existing: true, filePath: "bad.py", savedContent: "x" },
            { id: "good", content: "x", existing: false, filePath: "good.py", savedContent: "" },
          ],
        },
      },
    })).toEqual({
      project: {
        activeId: "good",
        primaryId: "good",
        secondaryId: undefined,
        buffers: [{ id: "good", content: "x", existing: false, filePath: "good.py", restoreFromCore: false, savedContent: "" }],
      },
    });
  });
});
