import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ListRow, ProgressState, SectionHeader, SettingsGroup, StandardEmptyState, StatusChip, Surface, SurfaceNotice, SurfacePanel, SurfaceState, TabBar, Toolbar } from "./SurfacePrimitives";

describe("shared surface primitives", () => {
  it("uses non-blocking semantics for informational notices and supports dismissal", async () => {
    const onDismiss = vi.fn();
    render(<SurfaceNotice title="Capability unavailable" detail="The rest of the workspace is usable." onDismiss={onDismiss} />);

    expect(screen.getByRole("status")).toHaveTextContent("Capability unavailable");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Dismiss notice" }));
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it("renders the standard empty-state decisions in a single action group", () => {
    render(<StandardEmptyState title="Nothing here" explanation="Create the first record." primaryAction={<button>Create</button>} secondaryAction={<button>Learn more</button>} />);
    expect(screen.getByText("Nothing here")).toBeVisible();
    expect(screen.getByRole("button", { name: "Create" }).parentElement).toHaveClass("empty-state-actions");
  });

  it("reports a Settings group only when it is opened", async () => {
    const onOpen = vi.fn();
    render(<SettingsGroup id="models-settings" title="Models" summary="Optional" open={false} onOpen={onOpen}><p>Models body</p></SettingsGroup>);
    await userEvent.click(screen.getByText("Models"));
    expect(onOpen).toHaveBeenCalledWith("models-settings");
  });

  it("shares section, panel, status, and lifecycle state anatomy", () => {
    render(<SurfacePanel labelledBy="health-heading"><SectionHeader id="health-heading" title="Current health" description="Everything is available." /><StatusChip tone="success">Ready</StatusChip><SurfaceState kind="loading" title="Loading records" explanation="This should only take a moment." compact /></SurfacePanel>);
    expect(screen.getByRole("region", { name: "Current health" })).toHaveClass("surface-panel");
    expect(screen.getByText("Ready")).toHaveClass("tone-success");
    expect(screen.getByText("Loading records").closest("section")).toHaveClass("state-loading");
  });

  it("provides authoritative surface, toolbar, row, and progress anatomy", () => {
    render(<Surface variant="grouped" labelledBy="surface-title"><h2 id="surface-title">Project files</h2><Toolbar label="File actions" primaryAction={<button>Upload</button>}>12 files</Toolbar><ListRow title="scope.txt" metadata="12 KB" status={<StatusChip>Indexed</StatusChip>} actions={<button>More</button>} /><ProgressState stage="Preparing runtime" detail="Downloading packages" elapsed="2m" progress={42} /></Surface>);
    expect(screen.getByRole("region", { name: "Project files" })).toHaveClass("surface-grouped");
    expect(screen.getByRole("toolbar", { name: "File actions" })).toHaveTextContent("12 files");
    expect(screen.getByRole("progressbar", { name: "Preparing runtime progress" })).toHaveAttribute("aria-valuenow", "42");
  });

  it("moves tab selection with the arrow keys", async () => {
    const onChange = vi.fn();
    render(<TabBar label="Tools" value="terminal" onChange={onChange} items={[{ id: "terminal", label: "Terminal" }, { id: "code", label: "Code" }]} />);
    const terminal = screen.getByRole("tab", { name: "Terminal" });
    terminal.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(onChange).toHaveBeenCalledWith("code");
  });
});
