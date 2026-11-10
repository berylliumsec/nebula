import {act, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {beforeEach, describe, expect, it, vi} from "vitest";
import type {EngagementScopePolicy, McpServerProfile} from "../api/types";
import {logCaughtDiagnostic} from "../diagnostics";
import {McpServerChoices} from "./McpServerChoices";

vi.mock("../diagnostics", () => ({logCaughtDiagnostic: vi.fn()}));

function server(id: string, tools: number): McpServerProfile {
  return {id, name: id, tools: Array.from({length: tools}, (_, index) => ({name: `tool-${index}`}))} as unknown as McpServerProfile;
}

const servers = [server("github", 24), server("linear", 11)];
const getEngagementScope = vi.fn();
const api = {getEngagementScope};

function scope(changes: Partial<EngagementScopePolicy>) {
  return {engagementId: "project-1", toolSuggestions: false, onDemandTools: true, ...changes} as EngagementScopePolicy;
}

describe("MCP server choices", () => {
  beforeEach(() => vi.resetAllMocks());

  it("sends ticked servers with every message and offers the rest on demand", async () => {
    getEngagementScope.mockResolvedValue(scope({}));
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<McpServerChoices api={api} projectId="project-1" servers={servers} selectedIds={["github"]} disabled={false} onChange={onChange} />);

    await waitFor(() => expect(getEngagementScope).toHaveBeenCalledWith("project-1"));
    expect(screen.getByText(/Unchecking excludes all their tools next turn/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", {name: /^github\s*24 tools · Always loaded$/})).toBeChecked();
    expect(screen.getByRole("checkbox", {name: /^linear\s*11 tools · On demand$/})).not.toBeChecked();

    await user.click(screen.getByRole("checkbox", {name: /^linear/}));
    expect(onChange).toHaveBeenCalledWith(["github", "linear"]);
    await user.click(screen.getByRole("checkbox", {name: /^github/}));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it("says unticked servers are not sent when the project turned on-demand tools off", async () => {
    getEngagementScope.mockResolvedValue(scope({onDemandTools: false}));
    render(<McpServerChoices api={api} projectId="project-1" servers={servers} selectedIds={["github"]} disabled={false} onChange={vi.fn()} />);

    expect(await screen.findByText("On-demand tools are off for this project, so only checked servers are sent.")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", {name: /^linear\s*11 tools · Not sent$/})).toBeInTheDocument();
    expect(screen.getByRole("checkbox", {name: /^github\s*24 tools · Always loaded$/})).toBeInTheDocument();
  });

  it("keeps servers on demand when Jev is on, since Jev implies deferral", async () => {
    getEngagementScope.mockResolvedValue(scope({onDemandTools: false, toolSuggestions: true}));
    render(<McpServerChoices api={api} projectId="project-1" servers={servers} selectedIds={[]} disabled={false} onChange={vi.fn()} />);

    // Let the scope land first: "On demand" is also what shows before it does.
    await act(async () => { await getEngagementScope.mock.results[0]?.value; });
    expect(screen.getByRole("checkbox", {name: /^linear\s*11 tools · On demand$/})).toBeInTheDocument();
  });

  it("falls back to Core's on-demand default when the scope cannot be read", async () => {
    getEngagementScope.mockRejectedValue(new Error("offline"));
    render(<McpServerChoices api={api} projectId="project-1" servers={servers} selectedIds={[]} disabled={false} onChange={vi.fn()} />);

    await waitFor(() => expect(logCaughtDiagnostic).toHaveBeenCalled());
    expect(screen.getByRole("checkbox", {name: /^github\s*24 tools · On demand$/})).toBeInTheDocument();
  });

  it("shows no hint when no server is enabled", () => {
    render(<McpServerChoices servers={[]} selectedIds={[]} disabled={false} onChange={vi.fn()} />);

    expect(screen.getByText("No enabled MCP profiles")).toBeInTheDocument();
    expect(screen.queryByText(/sent with every message/)).not.toBeInTheDocument();
  });

  it("keeps an unselected project Notebook grant out of on-demand tools", async () => {
    getEngagementScope.mockResolvedValue(scope({onDemandTools: true}));
    render(<McpServerChoices api={api} projectId="project-1" servers={[{...server("Notebook", 11), projectId: "project-1"}]} selectedIds={[]} disabled={false} onChange={vi.fn()} />);
    expect(await screen.findByRole("checkbox", {name: /Notebook\s*11 tools · Select to grant this assistant/})).not.toBeChecked();
  });

  it("shows excluded servers as not sent and restores through the existing checkbox", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<McpServerChoices servers={servers} selectedIds={[]} excludedIds={["github"]} disabled={false} onChange={onChange} />);
    const checkbox = screen.getByRole("checkbox", {name: /github.*Excluded · Not sent next turn/});
    expect(checkbox).not.toBeChecked();
    await user.click(checkbox);
    expect(onChange).toHaveBeenCalledWith(["github"]);
  });

  it("can exclude an untouched on-demand server without checking it first", async () => {
    const user = userEvent.setup();
    const onExclude = vi.fn();
    render(<McpServerChoices servers={servers} selectedIds={[]} disabled={false} onChange={vi.fn()} onExclude={onExclude} />);
    await user.click(screen.getByRole("button", {name: "Exclude linear next turn"}));
    expect(onExclude).toHaveBeenCalledWith("linear");
  });
});
