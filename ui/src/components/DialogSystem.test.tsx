import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DialogProvider, ModalSurface, useConfirmation, useDialogOpen } from "./DialogSystem";

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

function ConfirmationHarness({ onConfirm }: { onConfirm: (remember: boolean) => void }) {
  const confirm = useConfirmation();
  const [answer, setAnswer] = useState<string>();
  return <>
    <button type="button" onClick={() => void confirm({
      title: "Share redacted tool results?",
      message: "Allow this turn to send bounded tool inputs and results?",
      confirmLabel: "Allow this turn",
      remember: { label: "Always allow for Cloud runtime", hint: "Revoke it in Settings.", onConfirm },
    }).then((value) => setAnswer(value ? "allowed" : "declined"))}>Ask</button>
    <output aria-label="Answer">{answer}</output>
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


describe("confirmation standing consent", () => {
  it("reports the checked box only when the operator confirms", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<DialogProvider><ConfirmationHarness onConfirm={onConfirm} /></DialogProvider>);

    await user.click(screen.getByRole("button", { name: "Ask" }));
    await user.click(await screen.findByRole("checkbox", { name: /Always allow for Cloud runtime/ }));
    await user.click(screen.getByRole("button", { name: "Allow this turn" }));

    expect(onConfirm).toHaveBeenCalledWith(true);
    expect(screen.getByLabelText("Answer")).toHaveTextContent("allowed");

    // A cancelled turn records nothing, and the box starts clear next time.
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByRole("checkbox", { name: /Always allow for Cloud runtime/ })).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Answer")).toHaveTextContent("declined");
  });
});

it("does not steal focus when an operator reaches another field before initial focus runs", () => {
  let focusFrame: FrameRequestCallback | undefined;
  const request = vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => {focusFrame = callback; return 1;});
  const cancel = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
  try {
    const {unmount} = render(<ModalSurface labelledBy="focus-title" onClose={() => undefined}><h2 id="focus-title">Edit context</h2><button>Close</button><textarea aria-label="Immediate editing" /></ModalSurface>);
    const field = screen.getByRole("textbox", {name: "Immediate editing"});
    field.focus(); focusFrame?.(0);
    expect(field).toHaveFocus();
    unmount(); expect(cancel).toHaveBeenCalledWith(1);
  } finally {request.mockRestore(); cancel.mockRestore();}
});
