import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ButlerProjectPicker } from "./ButlerProjectPicker";

const groups = {
  active: [{ project: { id: "bb", name: "Apple Bug Bounty" }, label: "14 working" }, { project: { id: "sim", name: "Sim Lab" }, label: "2 need you" }],
  quiet: [{ id: "tv", name: "Apple TV fuzzing" }, { id: "notes", name: "Notes" }],
};

describe("Butler mode project picker", () => {
  it("filters projects as the operator types and keeps Active before Quiet", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<ButlerProjectPicker groups={groups} onSelect={onSelect} />);

    await user.click(screen.getByRole("button", { name: "Project thread: choose a project" }));
    expect(screen.getByRole("group", { name: "Active" })).toHaveTextContent("Apple Bug Bounty14 working");
    expect(screen.getByRole("group", { name: "Quiet" })).toHaveTextContent("Notes");

    await user.type(screen.getByRole("combobox", { name: "Search projects" }), "apple");
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["Apple Bug Bounty14 working", "Apple TV fuzzing"]);
    expect(screen.getByRole("status")).toHaveTextContent("2 of 4");

    await user.click(screen.getByRole("option", { name: "Apple TV fuzzing" }));
    expect(onSelect).toHaveBeenCalledWith("tv");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("moves with the arrow keys, chooses with Enter and closes with Esc", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<ButlerProjectPicker groups={groups} value="sim" onSelect={onSelect} />);
    const trigger = screen.getByRole("button", { name: "Project thread: Sim Lab" });

    // Typing on the closed picker starts the search with that letter.
    trigger.focus();
    await user.keyboard("n");
    const search = screen.getByRole("combobox", { name: "Search projects" });
    expect(search).toHaveValue("n");
    expect(search).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("option", { name: /Sim Lab/ })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}{ArrowDown}{ArrowUp}{ArrowDown}");
    expect(screen.getByRole("combobox", { name: "Search projects" })).toHaveAttribute("aria-activedescendant", screen.getByRole("option", { name: "Apple TV fuzzing" }).id);
    await user.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith("tv");
  });

  it("says when nothing matches", async () => {
    const user = userEvent.setup();
    render(<ButlerProjectPicker groups={groups} onSelect={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /Project thread/ }));
    await user.type(screen.getByRole("combobox", { name: "Search projects" }), "zzz");
    expect(screen.getByText("No projects match “zzz”.")).toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });
});
