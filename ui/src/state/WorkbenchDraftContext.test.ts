import { describe, expect, it } from "vitest";
import type { SelectionActionDraft } from "../components/selection";
import { ASSISTANT_CONTEXT_CHARACTER_LIMIT, mergeAssistantDraft, selectionHandoffMetadata } from "./WorkbenchDraftContext";

function draft(text: string, id: string): SelectionActionDraft {
  return {
    text,
    originalLength: text.length,
    truncated: false,
    source: { kind: "terminal", id, label: `Terminal ${id}` },
    anchor: { left: 0, top: 0, right: 0, bottom: 0 },
  };
}

describe("assistant context packs", () => {
  it("appends distinct exact selections and deduplicates identical sources and bytes", () => {
    const first = draft("443/tcp open", "one");
    const second = draft("certificate expired", "two");
    const merged = mergeAssistantDraft([first], second);
    expect(merged.drafts).toEqual([first, second]);
    const duplicate = mergeAssistantDraft(merged.drafts, second);
    expect(duplicate.drafts).toBe(merged.drafts);
    expect(duplicate.notice).toMatch(/already in the context pack/);
  });

  it("bounds the aggregate pack without splitting a surrogate pair", () => {
    const current = [draft("x".repeat(ASSISTANT_CONTEXT_CHARACTER_LIMIT - 1), "one")];
    const merged = mergeAssistantDraft(current, draft("😀tail", "two"));
    expect(merged.drafts).toEqual(current);
    expect(merged.notice).toMatch(/full/);
  });

  it("marks the newest item truncated when only a bounded suffix fits", () => {
    const current = [draft("x".repeat(ASSISTANT_CONTEXT_CHARACTER_LIMIT - 3), "one")];
    const merged = mergeAssistantDraft(current, draft("abcde", "two"));
    expect(merged.drafts[1]).toMatchObject({ text: "abc", truncated: true });
    expect(merged.drafts.reduce((total, item) => total + item.text.length, 0)).toBe(ASSISTANT_CONTEXT_CHARACTER_LIMIT);
  });
});

describe("selection handoff privacy", () => {
  it("persists only the source reference, label, and selected-byte hash", () => {
    const selected = draft("authorization: Bearer secret", "command-1");
    const metadata = selectionHandoffMetadata("project-1", selected);
    expect(metadata.sourceRefs).toEqual([{
      projectId: "project-1",
      kind: "terminal_session",
      id: "command-1",
    }]);
    expect(metadata.sourceLabels).toEqual({ "terminal_session:command-1": "Terminal command-1" });
    expect(metadata.sourceHashes["terminal_session:command-1"]).toMatch(/^[a-f0-9]{64}$/);
    expect(JSON.stringify(metadata)).not.toContain(selected.text);
    expect(JSON.stringify(metadata)).not.toContain("secret");
  });
});
