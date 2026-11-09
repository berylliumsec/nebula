import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { NavigationContinuity } from "./NavigationContinuity";
import { rememberChat, rememberedNavigation } from "./navigationContinuity";

const fixture = vi.hoisted(() => ({workspace: {} as any}));
vi.mock("./WorkspaceContext", () => ({useWorkspace: () => fixture.workspace}));
const scope = {key: "core:operator", persistent: true};
function Probe() {
  const location = useLocation(); const navigate = useNavigate();
  return <><output data-testid="location">{location.pathname}{location.search}{location.hash}</output><button onClick={() => navigate("/settings")}>Settings</button><button onClick={() => navigate("/")}>Home</button><NavigationContinuity><p>Workspace mounted</p></NavigationContinuity></>;
}
function mount(path = "/") { return render(<MemoryRouter initialEntries={[path]}><Probe /></MemoryRouter>); }
beforeEach(() => {
  vi.restoreAllMocks(); localStorage.clear();
  fixture.workspace = {continuityScope: scope, engagement: {id: "project"}, engagements: [{id: "project"}], resourceStatus: {projects: {state: "ready"}}, workspaceState: "ready"};
});

it("withholds the default route until identity and projects are known, then restores the exact older chat", async () => {
  rememberChat(scope, "project", "older-chat");
  fixture.workspace = {...fixture.workspace, workspaceState: "bootstrapping", continuityScope: undefined};
  const view = mount();
  expect(screen.getByText("Opening workspace…")).toBeVisible();
  expect(screen.queryByText("Workspace mounted")).toBeNull();
  fixture.workspace = {...fixture.workspace, workspaceState: "ready", continuityScope: scope};
  view.rerender(<MemoryRouter><Probe /></MemoryRouter>);
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects/project/workbench?view=chat&session=older-chat"));
});

it("preserves an explicit canonical deep link including its hash", async () => {
  rememberChat(scope, "project", "older-chat");
  mount("/projects/project/workbench?view=chat&session=explicit-chat#message");
  expect(screen.getByTestId("location")).toHaveTextContent("session=explicit-chat#message");
  await waitFor(() => expect(rememberedNavigation(scope)?.sessionId).toBe("explicit-chat"));
});

it("preserves an explicit root query instead of restoring remembered navigation", () => {
  rememberChat(scope, "project", "older-chat"); mount("/?view=chat");
  expect(screen.getByTestId("location")).toHaveTextContent("/?view=chat");
  expect(screen.getByText("Workspace mounted")).toBeVisible();
});

it("does not restore a deleted or inaccessible project", () => {
  rememberChat(scope, "unavailable", "older-chat"); mount();
  expect(screen.getByTestId("location")).toHaveTextContent("/");
  expect(screen.getByText("Workspace mounted")).toBeVisible();
});

it.each(["connection", "catalog"])("keeps the original entry pending through a %s failure", async kind => {
  rememberChat(scope, "project", "older-chat");
  fixture.workspace = {...fixture.workspace,
    workspaceState: kind === "connection" ? "failed" : "degraded",
    continuityScope: kind === "connection" ? undefined : scope,
    resourceStatus: {projects: {state: "failed"}}};
  const view = mount();
  expect(screen.getByText("Workspace mounted")).toBeVisible();
  fixture.workspace = {...fixture.workspace, workspaceState: "ready", continuityScope: scope, resourceStatus: {projects: {state: "ready"}}};
  view.rerender(<MemoryRouter><Probe /></MemoryRouter>);
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("session=older-chat"));
});

it.each(["other-core:operator", "core:device:other"])("does not restore another scope %s", key => {
  rememberChat(scope, "project", "older-chat"); fixture.workspace.continuityScope = {...scope, key}; mount();
  expect(screen.getByText("Workspace mounted")).toBeVisible();
  expect(screen.getByTestId("location")).toHaveTextContent("/");
});

it("never restores over navigation during bootstrap or a later Home visit", async () => {
  rememberChat(scope, "project", "older-chat"); fixture.workspace.workspaceState = "bootstrapping";
  const view = mount(); fireEvent.click(screen.getByText("Settings"));
  fixture.workspace.workspaceState = "ready"; view.rerender(<MemoryRouter><Probe /></MemoryRouter>);
  fireEvent.click(screen.getByText("Home"));
  await waitFor(() => expect(screen.getByText("Workspace mounted")).toBeVisible());
  expect(screen.getByTestId("location")).toHaveTextContent("/");
});

it("does not record a route while the selected project belongs to the previous render", () => {
  rememberChat(scope, "project", "older-chat"); mount("/projects/other/workbench?view=chat&session=other-chat");
  expect(rememberedNavigation(scope)?.sessionId).toBe("older-chat");
});

it("reports a persistence limitation without blocking explicit navigation", async () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage blocked"); });
  mount("/projects/project/workbench?view=chat&session=older-chat");
  await waitFor(() => expect(screen.getByText(/This device could not remember/)).toBeVisible());
  expect(screen.getByText("Workspace mounted")).toBeVisible();
});
