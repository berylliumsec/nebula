import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {describe, expect, it, vi} from "vitest";
import type {McpServerProfile} from "../api/types";
import {McpServerChoices} from "./McpServerChoices";

const servers = [{id: "notes", name: "Notes", tools: [{name: "read"}]}] as McpServerProfile[];

describe("MCP tool context mode", () => {
  it("shows a compact accessible on-demand choice and selected server state", async () => {
    const user = userEvent.setup();
    const onContextModeChange = vi.fn();
    render(<McpServerChoices servers={servers} selectedIds={["notes"]} contextMode="on_demand" disabled={false} onChange={vi.fn()} onContextModeChange={onContextModeChange} />);
    const select = screen.getByRole("combobox", {name: "MCP tool context"});
    expect(select).toHaveValue("on_demand");
    expect(screen.getByRole("checkbox", {name: /^Notes\s*1 tools · On demand$/})).toBeChecked();
    expect(screen.getByText(/Brief tool directory only/)).toBeInTheDocument();
    await user.selectOptions(select, "always_loaded");
    expect(onContextModeChange).toHaveBeenCalledWith("always_loaded");
    // Controlled state waits for the parent's observed Core value.
    expect(select).toHaveValue("on_demand");
  });

  it("makes full schema loading an explicit choice and disables pending mutations", () => {
    const {rerender} = render(<McpServerChoices servers={servers} selectedIds={["notes"]} contextMode="always_loaded" disabled={false} onChange={vi.fn()} onContextModeChange={vi.fn()} />);
    expect(screen.getByRole("combobox", {name: "MCP tool context"})).toHaveValue("always_loaded");
    expect(screen.getByRole("checkbox", {name: /^Notes\s*1 tools · Always loaded$/})).toBeChecked();
    rerender(<McpServerChoices servers={servers} selectedIds={["notes"]} contextMode="on_demand" disabled onChange={vi.fn()} onContextModeChange={vi.fn()} />);
    expect(screen.getByRole("combobox", {name: "MCP tool context"})).toBeDisabled();
    expect(screen.getByRole("checkbox")).toBeDisabled();
  });

  it("keeps excluded sources absent in either mode and does not offer a conflicting exclusion", () => {
    const {rerender} = render(<McpServerChoices servers={servers} selectedIds={[]} excludedIds={["notes"]} contextMode="on_demand" disabled={false} onChange={vi.fn()} onExclude={vi.fn()} />);
    expect(screen.getByRole("checkbox", {name: /Excluded · Not sent next turn/})).not.toBeChecked();
    rerender(<McpServerChoices servers={servers} selectedIds={["notes"]} contextMode="on_demand" disabled={false} onChange={vi.fn()} onExclude={vi.fn()} />);
    expect(screen.queryByRole("button", {name: /Exclude/})).not.toBeInTheDocument();
  });

  it("still exposes the mode for the empty catalog", () => {
    render(<McpServerChoices servers={[]} selectedIds={[]} contextMode="on_demand" disabled={false} onChange={vi.fn()} onContextModeChange={vi.fn()} />);
    expect(screen.getByRole("combobox", {name: "MCP tool context"})).toBeEnabled();
    expect(screen.getByText("No enabled MCP profiles")).toBeInTheDocument();
  });
});
