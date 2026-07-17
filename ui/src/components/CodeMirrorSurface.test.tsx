import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CodeMirrorSurface } from "./CodeMirrorSurface";

describe("CodeMirrorSurface", () => {
  it("loads language highlighting and keeps the editable surface unboxed", async () => {
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

    await waitFor(() => {
      const importToken = [...view.container.querySelectorAll(".cm-line span")]
        .find((element) => element.textContent === "import");
      expect(importToken?.className).toBeTruthy();
    });

    const editable = view.container.querySelector<HTMLElement>(".cm-content");
    expect(editable).not.toBeNull();
    editable?.focus();
    expect(getComputedStyle(editable!).outlineStyle).toBe("none");
    expect(view.container.querySelectorAll(".cm-editor")).toHaveLength(1);
  });
});
