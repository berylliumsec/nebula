import "fake-indexeddb/auto";
import { afterEach, describe, expect, it } from "vitest";
import { clearEditorSessions, loadEditorSessions, normalizeEditorSessions, saveEditorSessions } from "./editorSessionPersistence";

afterEach(() => clearEditorSessions());

describe("editorSessionPersistence", () => {
  const draft = (index: number, content = `draft ${index}`) => ({ id: `draft-${index}`, filePath: `draft-${index}.txt`, content, savedContent: "", existing: false });

  it("recovers every draft and the active 21st buffer across more than 50 projects", async () => {
    const sessions = Object.fromEntries(Array.from({ length: 51 }, (_, project) => [`project-${project}`, {
      activeId: "draft-20", primaryId: "draft-20", buffers: Array.from({ length: 21 }, (_, index) => draft(index)),
    }]));
    await saveEditorSessions(sessions);
    const restored = await loadEditorSessions();
    expect(Object.keys(restored)).toHaveLength(51);
    for (const session of Object.values(restored)) {
      expect(session.buffers).toHaveLength(21);
      expect(session.activeId).toBe("draft-20");
      expect(session.buffers.map((buffer) => buffer.content)).toEqual(sessions["project-0"].buffers.map((buffer) => buffer.content));
    }
  });

  it("rejects aggregate overflow atomically and allows recovery to be retried", async () => {
    await saveEditorSessions({ project: { buffers: [draft(0)] } });
    const previous = await loadEditorSessions();
    const oversized = { project: { buffers: Array.from({ length: 9 }, (_, index) => draft(index, "x".repeat(1024 * 1024))) } };
    await expect(saveEditorSessions(oversized)).rejects.toThrow("8 MiB");
    await expect(loadEditorSessions()).resolves.toEqual(previous);
    await saveEditorSessions({ project: { buffers: [draft(1, "latest draft")] } });
    expect((await loadEditorSessions()).project.buffers[0].content).toBe("latest draft");
  });

  it("rejects oversized individual drafts without replacing a valid recovery snapshot", async () => {
    await saveEditorSessions({ project: { buffers: [draft(0)] } });
    const previous = await loadEditorSessions();
    await expect(saveEditorSessions({ project: { buffers: [draft(1, "λ".repeat(600_000))] } })).rejects.toThrow("previous recovery snapshot is unchanged");
    await expect(loadEditorSessions()).resolves.toEqual(previous);
  });

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
