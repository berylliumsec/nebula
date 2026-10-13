import { act, render } from "@testing-library/react";
import { EditorView } from "@codemirror/view";
import { describe, expect, it, vi } from "vitest";
import { CodeMirrorSurface } from "./CodeMirrorSurface";

describe("CodeMirror value synchronization", () => {
  it("keeps newer typing and its caret when an older React value arrives", () => {
    Object.defineProperty(document, "fonts", { configurable: true, value: { ready: Promise.resolve() } });
    const onChange = vi.fn();
    const surface = (value: string) => <CodeMirrorSurface
      active
      filePath="example.c"
      value={value}
      onChange={onChange}
      onCursorChange={() => {}}
      onSave={() => {}}
    />;
    const result = render(surface(""));
    const editor = result.container.querySelector(".code-mirror-host")?.shadowRoot?.querySelector(".cm-editor");
    const view = editor && EditorView.findFromDOM(editor as HTMLElement);
    expect(view).toBeTruthy();

    act(() => view!.dispatch({ changes: { from: 0, insert: "a" }, selection: { anchor: 1 } }));
    act(() => view!.dispatch({ changes: { from: 1, insert: "b" }, selection: { anchor: 2 } }));
    expect(onChange.mock.calls.map(([content]) => content)).toEqual(["a", "ab"]);

    result.rerender(surface("a"));
    expect(view!.state.doc.toString()).toBe("ab");
    expect(view!.state.selection.main.head).toBe(2);

    result.rerender(surface("ab"));
    expect(view!.state.doc.toString()).toBe("ab");

    result.rerender(surface("reloaded from Core"));
    expect(view!.state.doc.toString()).toBe("reloaded from Core");
    expect(onChange).toHaveBeenCalledTimes(2);
  });
});
