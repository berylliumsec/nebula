import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { settingCatalog } from "../settingsCatalog";
import { DialogProvider, ModalSurface } from "./DialogSystem";
import { SettingsLens } from "./SettingsLens";

vi.mock("../pages/SettingsPage", () => ({
  SettingsPage: ({ embeddedTarget }: { embeddedTarget?: string }) => <section><h3>Embedded {embeddedTarget}</h3><button type="button">Embedded control</button></section>,
}));

const entry = settingCatalog.find((item) => item.target === "provider-settings") ?? settingCatalog[0];

describe("settings lens keyboard handling", () => {
  it("takes focus when it opens so Escape closes it before any click", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<MemoryRouter><DialogProvider><SettingsLens entry={entry} onClose={onClose} /></DialogProvider></MemoryRouter>);

    const lens = screen.getByRole("dialog", { name: entry.label });
    await waitFor(() => expect(lens.contains(document.activeElement)).toBe(true));
    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("leaves Escape to a modal dialog opened above it", async () => {
    const onClose = vi.fn();
    const closeModal = vi.fn();
    const user = userEvent.setup();
    render(<MemoryRouter><DialogProvider>
      <SettingsLens entry={entry} onClose={onClose} />
      <ModalSurface labelledBy="above-title" onClose={closeModal}><h2 id="above-title">Above the lens</h2><button type="button">Continue</button></ModalSurface>
    </DialogProvider></MemoryRouter>);

    await screen.findByText(`Embedded ${entry.target}`);
    await user.keyboard("{Escape}");

    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
  });
});
