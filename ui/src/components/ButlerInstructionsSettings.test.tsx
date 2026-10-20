import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {ButlerInstructionsSettings} from "./ButlerInstructionsSettings";

vi.mock("./CodeMirrorSurface", () => ({
  CodeMirrorSurface: ({ariaLabel, value, onChange, onSave}: {ariaLabel: string; value: string; onChange(value: string): void; onSave(): void}) => <textarea aria-label={ariaLabel} value={value} onChange={(event) => onChange(event.target.value)} onKeyDown={(event) => {if (event.key === "s" && (event.ctrlKey || event.metaKey)) onSave();}} />,
}));

const path = "/data/nebula/.butler/BUTLER.md";

describe("ButlerInstructionsSettings", () => {
  it("creates the user file and confirms Core's readback before showing saved", async () => {
    const user = userEvent.setup();
    const missing = {path, status: "missing", content: null, sha256: null, detail: null};
    const ready = {path, status: "ready", content: "Inspect Act projects.", sha256: "a".repeat(64), detail: null};
    const request = vi.fn()
      .mockResolvedValueOnce(missing)
      .mockResolvedValueOnce(ready)
      .mockResolvedValueOnce(ready);
    render(<ButlerInstructionsSettings api={{request} as unknown as ApiClient} />);
    expect(await screen.findByText(/No instructions file yet/)).toBeVisible();
    expect(screen.getByText(path)).toBeVisible();
    await user.click(screen.getByRole("button", {name: "Create BUTLER.md in editor"}));
    expect(screen.getByRole("dialog", {name: "BUTLER.md"})).toBeVisible();
    await user.type(screen.getByRole("textbox", {name: "BUTLER.md"}), "Inspect Act projects.");
    await user.click(screen.getByRole("button", {name: "Preview Markdown"}));
    expect(screen.getByRole("region", {name: "Markdown preview: BUTLER.md"})).toHaveTextContent("Inspect Act projects.");
    await user.click(screen.getByRole("button", {name: "Save to Core"}));
    await waitFor(() => expect(request).toHaveBeenNthCalledWith(2, "butler/instructions", {
      method: "PUT", body: JSON.stringify({content: "Inspect Act projects.", expected_sha256: null}),
    }));
    expect(await screen.findByText(/Saved on Core/)).toBeVisible();
    await user.click(screen.getByRole("button", {name: "Edit Markdown"}));
    expect(screen.getByRole("textbox", {name: "BUTLER.md"})).toHaveValue("Inspect Act projects.");
    expect(screen.getByRole("button", {name: "Save to Core"})).toBeDisabled();
    await user.click(screen.getByRole("button", {name: "Close Butler editor"}));
    await user.click(screen.getByRole("button", {name: "Open BUTLER.md in editor"}));
    expect(screen.getByRole("region", {name: "Markdown preview: BUTLER.md"})).toHaveTextContent("Inspect Act projects.");
  });

  it("keeps an unsaved draft after a conflict until the operator reloads", async () => {
    const user = userEvent.setup();
    const initial = {path, status: "ready", content: "First version.", sha256: "a".repeat(64), detail: null};
    const latest = {path, status: "ready", content: "Newer version.", sha256: "b".repeat(64), detail: null};
    const request = vi.fn()
      .mockResolvedValueOnce(initial)
      .mockRejectedValueOnce(new Error("BUTLER.md changed since you loaded it"))
      .mockResolvedValueOnce(latest);
    render(<ButlerInstructionsSettings api={{request} as unknown as ApiClient} />);
    await user.click(await screen.findByRole("button", {name: "Open BUTLER.md in editor"}));
    expect(screen.getByRole("region", {name: "Markdown preview: BUTLER.md"})).toHaveTextContent("First version.");
    await user.click(screen.getByRole("button", {name: "Edit Markdown"}));
    const editor = screen.getByRole("textbox", {name: "BUTLER.md"});
    await user.clear(editor);
    await user.type(editor, "My draft.");
    await user.click(screen.getByRole("button", {name: "Save to Core"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("changed elsewhere");
    expect(editor).toHaveValue("My draft.");
    await user.click(screen.getByRole("button", {name: "Discard edits and reload"}));
    await waitFor(() => expect(screen.getByRole("region", {name: "Markdown preview: BUTLER.md"})).toHaveTextContent("Newer version."));
  });

  it("opens an uploaded draft and keeps it when the editor is closed accidentally", async () => {
    const user = userEvent.setup();
    const missing = {path, status: "missing", content: null, sha256: null, detail: null};
    const request = vi.fn().mockResolvedValue(missing);
    render(<ButlerInstructionsSettings api={{request} as unknown as ApiClient} />);
    await screen.findByText(/No instructions file yet/);
    await user.upload(screen.getByLabelText("Upload Butler Markdown file"), new File(["# My Butler\n"], "BUTLER.md", {type: "text/markdown"}));
    expect(screen.getByRole("textbox", {name: "BUTLER.md"})).toHaveValue("# My Butler\n");
    await user.click(screen.getByRole("button", {name: "Close Butler editor"}));
    expect(screen.getByText("Discard unsaved changes?")).toBeVisible();
    await user.click(screen.getByRole("button", {name: "Keep editing"}));
    expect(screen.getByRole("textbox", {name: "BUTLER.md"})).toHaveValue("# My Butler\n");
  });
});
