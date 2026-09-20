import { EditorSelection, EditorState } from "@codemirror/state";
import { describe, expect, it } from "vitest";
import { activeIndentScope, bracketDepths, commentSyntaxForPath, indentColumns } from "./editorDecorations";

const C_LIKE = commentSyntaxForPath("view.tsx");
const PYTHON = commentSyntaxForPath("scan.py");

function state(doc: string, tabSize = 2, head = 0) {
  return EditorState.create({ doc, selection: EditorSelection.single(head), extensions: [EditorState.tabSize.of(tabSize)] });
}

describe("bracket depths", () => {
  it("numbers nesting depth from zero and matches closers to their opener", () => {
    const depths = bracketDepths("f(g(x))", C_LIKE);
    expect(depths.get(1)).toBe(0);
    expect(depths.get(3)).toBe(1);
    expect(depths.get(5)).toBe(1);
    expect(depths.get(6)).toBe(0);
  });

  it("ignores brackets inside strings and comments so real code keeps its colours", () => {
    const withString = bracketDepths("call(\"(\", 1)", C_LIKE);
    expect(withString.get(4)).toBe(0);
    expect(withString.get(6)).toBeUndefined();
    expect(withString.get(11)).toBe(0);

    const withComment = bracketDepths("call(1) // )\n[2]", C_LIKE);
    expect(withComment.get(11)).toBeUndefined();
    expect(withComment.get(13)).toBe(0);

    const hash = bracketDepths("call(1)  # )\n", PYTHON);
    expect(hash.get(11)).toBeUndefined();
  });

  it("marks brackets with no partner so a typo is visible", () => {
    expect(bracketDepths("open(", C_LIKE).get(4)).toBe(-1);
    expect(bracketDepths("stray)", C_LIKE).get(5)).toBe(-1);
  });

  it("escapes inside a string do not end it early", () => {
    const depths = bracketDepths("f(\"a\\\"(\", 2)", C_LIKE);
    expect(depths.get(1)).toBe(0);
    expect(depths.get(7)).toBeUndefined();
  });
});

describe("indent measurement", () => {
  it("expands tabs to the next tab stop and reports whitespace-only lines as contentless", () => {
    expect(indentColumns("    value", 2)).toBe(4);
    expect(indentColumns("\tvalue", 4)).toBe(4);
    expect(indentColumns("  \tvalue", 4)).toBe(4);
    expect(indentColumns("value", 2)).toBe(0);
    expect(indentColumns("   ", 2)).toBe(-1);
  });
});

describe("active indent scope", () => {
  const doc = [
    "const view = {",
    "  extensions: [",
    "    lineNumbers(),",
    "    minimap(),",
    "  ],",
    "};",
  ].join("\n");

  it("highlights the block a cursor sits inside", () => {
    const head = doc.indexOf("minimap");
    expect(activeIndentScope(state(doc, 2, head), 2)).toEqual({ level: 1, from: 3, to: 4 });
  });

  it("highlights the block a line opens when the cursor is on the opening line", () => {
    const head = doc.indexOf("extensions");
    expect(activeIndentScope(state(doc, 2, head), 2)).toEqual({ level: 1, from: 3, to: 4 });
  });

  it("has nothing to highlight at the outermost level", () => {
    expect(activeIndentScope(state("value = 1\n", 2, 0), 2)).toBeUndefined();
  });
});
