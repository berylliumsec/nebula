import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { SideToolSurface } from "./SideToolSurface";

function EditorFixture({ onClose = vi.fn() }: { onClose?: (id: string) => void }) {
  const [draft, setDraft] = useState("original");
  return <div>
    <button type="button">Outside pane</button>
    <SideToolSurface activeTab={{ id: "code-one", kind: "code", number: 1 }} visible onClose={onClose}>
      <textarea aria-label="Editor draft" value={draft} onChange={event => setDraft(event.target.value)} />
    </SideToolSurface>
  </div>;
}

describe("SideToolSurface fullscreen", () => {
  it("keeps an editor draft mounted while expanding, restoring, and using Escape", () => {
    const previousOverflow = document.body.style.overflow;
    render(<EditorFixture />);
    const draft = screen.getByRole("textbox", { name: "Editor draft" });
    const outside = screen.getByRole("button", { name: "Outside pane" });
    fireEvent.change(draft, { target: { value: "unsaved work" } });
    fireEvent.click(screen.getByRole("button", { name: "Expand Code 1 to full screen" }));
    expect(screen.getByRole("dialog", { name: "Code 1 full screen" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("textbox", { name: "Editor draft" })).toBe(draft);
    expect(draft).toHaveValue("unsaved work");
    expect(outside.inert).toBe(true);
    expect(document.body.style.overflow).toBe("hidden");

    fireEvent.click(screen.getByRole("button", { name: "Restore Code 1 to split view" }));
    expect(screen.getByRole("region", { name: "Code 1 beside chat" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Editor draft" })).toBe(draft);
    expect(outside.inert).not.toBe(true);
    expect(document.body.style.overflow).toBe(previousOverflow);

    fireEvent.click(screen.getByRole("button", { name: "Expand Code 1 to full screen" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByRole("region", { name: "Code 1 beside chat" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand Code 1 to full screen" })).toHaveFocus();
    expect(draft).toHaveValue("unsaved work");
  });

  it("restores background interaction when the active tool closes", () => {
    const onClose = vi.fn();
    render(<EditorFixture onClose={onClose} />);
    const outside = screen.getByRole("button", { name: "Outside pane" });
    fireEvent.click(screen.getByRole("button", { name: "Expand Code 1 to full screen" }));
    fireEvent.click(screen.getByRole("button", { name: "Close Code 1 tab" }));
    expect(onClose).toHaveBeenCalledWith("code-one");
    expect(outside.inert).not.toBe(true);
    expect(screen.getByRole("region", { name: "Code 1 beside chat" })).toBeInTheDocument();
  });
});
