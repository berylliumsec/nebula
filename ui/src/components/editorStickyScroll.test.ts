import { EditorState } from "@codemirror/state";
import { describe, expect, it } from "vitest";
import { enclosingScopes } from "./editorStickyScroll";

function state(doc: string, tabSize: number) {
  return EditorState.create({ doc, extensions: [EditorState.tabSize.of(tabSize)] });
}

const python = [
  "class WorkspaceScan:",
  "    def run(self, root):",
  "        if root.exists():",
  "            return scan(root)",
  "    def other(self):",
  "        return 0",
].join("\n");

describe("sticky scroll scopes", () => {
  it("lists the declarations enclosing a line, outermost first", () => {
    expect(enclosingScopes(state(python, 4), 4)).toEqual([1, 2, 3]);
  });

  it("keeps only the outermost declarations when nesting runs deeper than the limit", () => {
    expect(enclosingScopes(state(python, 4), 4, 2)).toEqual([1, 2]);
  });

  it("skips sibling statements that do not open a block", () => {
    const doc = [
      "function outer() {",
      "  setup();",
      "  if (ready) {",
      "    run();",
      "  }",
      "  teardown();",
      "  finish();",
    ].join("\n");
    expect(enclosingScopes(state(doc, 2), 7)).toEqual([1]);
  });

  it("has no scopes for a top-level line", () => {
    expect(enclosingScopes(state(python, 4), 1)).toEqual([]);
  });
});
