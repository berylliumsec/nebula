import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CodeMirrorSurface } from "./CodeMirrorSurface";
import "../refinement.css";
import "../zero-theme.css";

describe("CodeMirrorSurface", () => {
  it("loads language highlighting immediately and keeps the editable surface unboxed", () => {
    const view = render(
      <CodeMirrorSurface
        active
        filePath="example.py"
        value={'import requests\nprint("ready")\n'}
        onChange={vi.fn()}
        onCursorChange={vi.fn()}
        onSave={vi.fn()}
      />,
    );

    const importToken = [...view.container.querySelectorAll<HTMLElement>(".cm-line span")]
      .find((element) => element.textContent === "import");
    expect(importToken?.className).toBeTruthy();
    expect(getComputedStyle(importToken!).color).toBe("rgb(197, 134, 192)");

    const editable = view.container.querySelector<HTMLElement>(".cm-content");
    expect(editable).not.toBeNull();
    editable?.focus();
    expect(getComputedStyle(editable!).outlineStyle).toBe("none");
    expect(getComputedStyle(editable!).borderTopWidth).toBe("0px");
    expect(getComputedStyle(editable!).boxShadow).toBe("none");
    expect(view.container.querySelectorAll(".cm-editor")).toHaveLength(1);
  });
});
