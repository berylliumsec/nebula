import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { DialogProvider, ModalSurface, useDialogOpen } from "./DialogSystem";

function DialogHarness() {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" onClick={() => setOpen(true)}>Open dialog</button>
    {open && <ModalSurface as="form" labelledBy="dialog-title" onClose={() => setOpen(false)} onSubmit={(event) => event.preventDefault()}>
      <h2 id="dialog-title">Shared dialog</h2>
      <input data-autofocus aria-label="Dialog field" />
      <button type="submit">Save</button>
    </ModalSurface>}
  </>;
}

function DialogState() {
  return <output aria-label="Dialog state">{useDialogOpen() ? "open" : "closed"}</output>;
}

describe("ModalSurface", () => {
  it("focuses the requested control, closes on Escape, and restores focus", async () => {
    const user = userEvent.setup();
    render(<DialogHarness />);
    const opener = screen.getByRole("button", { name: "Open dialog" });
    await user.click(opener);
    const field = await screen.findByRole("textbox", { name: "Dialog field" });
    await waitFor(() => expect(field).toHaveFocus());

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("closes when the backdrop is pressed", async () => {
    const user = userEvent.setup();
    const { container } = render(<DialogHarness />);
    await user.click(screen.getByRole("button", { name: "Open dialog" }));
    await user.click(container.querySelector(".dialog-backdrop") as HTMLElement);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("publishes ordinary modal presence to native-surface consumers", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><DialogState /><DialogHarness /></DialogProvider>);
    expect(screen.getByLabelText("Dialog state")).toHaveTextContent("closed");
    await user.click(screen.getByRole("button", { name: "Open dialog" }));
    expect(screen.getByLabelText("Dialog state")).toHaveTextContent("open");
    await user.keyboard("{Escape}");
    expect(screen.getByLabelText("Dialog state")).toHaveTextContent("closed");
  });
});
