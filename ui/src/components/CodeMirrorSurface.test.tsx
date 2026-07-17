import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CodeMirrorSurface } from "./CodeMirrorSurface";
import "../refinement.css";
import "../zero-theme.css";

describe("CodeMirrorSurface", () => {
  it("renders C syntax in one editable DOM layer with an unboxed caret surface", () => {
    const view = render(
      <CodeMirrorSurface
        active
        filePath="example.c"
        value={"#include <stdio.h>\nint main(void) {\n  return 0;\n}\n"}
        onChange={vi.fn()}
        onCursorChange={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    const returnToken = [...view.container.querySelectorAll<HTMLElement>(".cm-line span")]
      .find((element) => element.textContent === "return");
    expect(returnToken?.className).toBeTruthy();

    const editable = view.container.querySelector<HTMLElement>(".cm-content");
    expect(editable).not.toBeNull();
    editable?.focus();
    expect(getComputedStyle(editable!).outlineStyle).toBe("none");
    expect(getComputedStyle(editable!).borderTopWidth).toBe("0px");
    expect(getComputedStyle(editable!).boxShadow).toBe("none");
    expect(view.container.querySelectorAll(".cm-editor")).toHaveLength(1);
  });
});
