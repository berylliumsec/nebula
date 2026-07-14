import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SelectionActionsProvider } from "./SelectionActionsProvider";
import {
  createHashedSelectionAttachment,
  createSelectionDraft,
  readDomSelection,
  readTextControlSelection,
  SELECTION_TEXT_LIMIT,
} from "./selectionActions";
import { bindXtermSelectionActions } from "./xtermSelectionAdapter";

function selectNodeText(node: Node, start = 0, end = node.textContent?.length ?? 0): Selection {
  const range = document.createRange();
  range.setStart(node, start);
  range.setEnd(node, end);
  const selection = document.getSelection() as Selection;
  selection.removeAllRanges();
  selection.addRange(range);
  return selection;
}

describe("selection actions", () => {
  it("bounds exact text and marks a truncated draft", () => {
    const text = `λ${"x".repeat(SELECTION_TEXT_LIMIT + 10)}`;
    const draft = createSelectionDraft({
      text,
      source: { kind: "test", label: "Test" },
    });
    expect(draft).toMatchObject({
      text: text.slice(0, SELECTION_TEXT_LIMIT),
      originalLength: text.length,
      truncated: true,
    });
  });

  it("does not split a Unicode surrogate pair at the limit", () => {
    const draft = createSelectionDraft({
      text: "a😀b",
      source: { kind: "test", label: "Test" },
    }, 2);
    expect(draft?.text).toBe("a");
    expect(draft?.truncated).toBe(true);
  });

  it("creates the exact SHA-256 chat attachment only on explicit conversion", async () => {
    const draft = createSelectionDraft({
      text: "λ selected",
      source: { kind: "terminal", id: "terminal-1", label: "Terminal" },
    });
    expect(await createHashedSelectionAttachment(draft as NonNullable<typeof draft>)).toEqual({
      sourceKind: "terminal",
      sourceId: "terminal-1",
      sourceLabel: "Terminal",
      text: "λ selected",
      sha256: "42892eb01060e5fa36ee4a1eb5d82344e09f38dc42a4d95f903174ebd54c0582",
      truncated: false,
    });
  });

  it("rejects password and explicitly sensitive DOM selections", () => {
    const password = document.createElement("input");
    password.type = "password";
    password.value = "never copy this";
    document.body.append(password);
    password.setSelectionRange(0, password.value.length);
    expect(readTextControlSelection(password)).toBeUndefined();

    const sensitive = document.createElement("span");
    sensitive.dataset.nebulaSensitive = "true";
    sensitive.textContent = "secret rendered value";
    document.body.append(sensitive);
    expect(readDomSelection(selectNodeText(sensitive.firstChild as Text))).toBeUndefined();
    password.remove();
    sensitive.remove();
  });

  it("opens draft-only Ask and Add note actions with source metadata", async () => {
    const user = userEvent.setup();
    const onAsk = vi.fn();
    const onAddNote = vi.fn();
    render(<SelectionActionsProvider onAsk={onAsk} onAddNote={onAddNote}>
      <p data-selection-source-kind="finding" data-selection-source-id="finding-7" data-selection-source-label="Finding 7">
        exact λ text
      </p>
    </SelectionActionsProvider>);
    const paragraph = screen.getByText("exact λ text");
    selectNodeText(paragraph.firstChild as Text, 6, 12);
    fireEvent.pointerUp(paragraph);
    expect(screen.getByRole("toolbar", { name: "Selected text actions" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Ask Nebula" }));
    expect(onAsk).toHaveBeenCalledWith(expect.objectContaining({
      text: "λ text",
      source: { kind: "finding", id: "finding-7", label: "Finding 7" },
    }));
    expect(onAddNote).not.toHaveBeenCalled();
    const exitingToolbar = screen.getByRole("toolbar", { name: "Selected text actions" });
    expect(exitingToolbar.className).toContain("exiting");
    fireEvent.animationEnd(exitingToolbar);
    await waitFor(() => expect(screen.queryByRole("toolbar", { name: "Selected text actions" })).toBeNull());

    selectNodeText(paragraph.firstChild as Text, 0, 5);
    fireEvent.pointerUp(paragraph);
    await user.click(screen.getByRole("button", { name: "Add note" }));
    expect(onAddNote).toHaveBeenCalledWith(expect.objectContaining({ text: "exact" }));
    await waitFor(() => expect(screen.queryByRole("toolbar", { name: "Selected text actions" })).toBeNull());
  });

  it("dismisses the selection actions after copying", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<SelectionActionsProvider onAsk={vi.fn()}>
      <p>copy this text</p>
    </SelectionActionsProvider>);
    const paragraph = screen.getByText("copy this text");
    selectNodeText(paragraph.firstChild as Text);
    fireEvent.pointerUp(paragraph);

    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledWith("copy this text");
    const toolbar = screen.getByRole("toolbar", { name: "Selected text actions" });
    expect(toolbar.className).toContain("exiting");
    fireEvent.animationEnd(toolbar);
    await waitFor(() => expect(screen.queryByRole("toolbar", { name: "Selected text actions" })).toBeNull());
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: originalClipboard });
  });

  it("offers mandatory reviewed Run only for terminal selections", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    const { unmount } = render(<SelectionActionsProvider onAsk={vi.fn()} onRun={onRun}>
      <code data-selection-source-kind="terminal" data-selection-source-id="terminal-1" data-selection-source-label="Terminal selection">whoami</code>
    </SelectionActionsProvider>);
    const terminal = screen.getByText("whoami");
    selectNodeText(terminal.firstChild as Text);
    fireEvent.pointerUp(terminal);
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
      text: "whoami",
      source: { kind: "terminal", id: "terminal-1", label: "Terminal selection" },
    }));
    unmount();

    render(<SelectionActionsProvider onAsk={vi.fn()} onRun={onRun}>
      <p data-selection-source-kind="finding" data-selection-source-label="Finding">do not execute prose</p>
    </SelectionActionsProvider>);
    const finding = screen.getByText("do not execute prose");
    selectNodeText(finding.firstChild as Text);
    fireEvent.pointerUp(finding);
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
  });

  it("adapts xterm's public selection API without sending it", async () => {
    let selectionListener: () => void = () => undefined;
    const present = vi.fn();
    const dispose = vi.fn();
    const terminal = {
      element: document.createElement("div"),
      getSelection: () => "whoami\n",
      hasSelection: () => true,
      onSelectionChange(listener: () => void) {
        selectionListener = listener;
        return { dispose };
      },
    };
    const binding = bindXtermSelectionActions(terminal, present, {
      source: { kind: "terminal", id: "terminal-1", label: "Terminal" },
    });
    selectionListener();
    await vi.waitFor(() => {
      expect(present).toHaveBeenCalledWith(expect.objectContaining({
        text: "whoami\n",
        source: { kind: "terminal", id: "terminal-1", label: "Terminal" },
      }));
    });
    binding.dispose();
    expect(dispose).toHaveBeenCalledOnce();
  });
});
