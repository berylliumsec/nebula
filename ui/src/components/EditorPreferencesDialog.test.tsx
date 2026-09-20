import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DEFAULT_EDITOR_PREFERENCES } from "../state/editorPreferences";
import { EditorPreferencesDialog } from "./EditorPreferencesDialog";

describe("EditorPreferencesDialog", () => {
  it("lets Tab and Shift+Tab leave a shortcut field while still capturing modified chords", async () => {
    const user = userEvent.setup();
    render(<EditorPreferencesDialog preferences={DEFAULT_EDITOR_PREFERENCES} onApply={vi.fn()} onClose={vi.fn()} />);
    const save = screen.getByRole("textbox", { name: "Save active file shortcut" });
    const palette = screen.getByRole("textbox", { name: "Show command palette shortcut" });

    save.focus();
    await user.tab();
    expect(palette).toHaveFocus();
    await user.tab({ shift: true });
    expect(save).toHaveFocus();
    expect(save).toHaveValue("Mod+S");

    await user.keyboard("{Control>}{Tab}{/Control}");
    expect(save).toHaveValue("Mod+Tab");
    expect(save).toHaveFocus();
  });
});
