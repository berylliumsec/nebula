import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { SecurityBrowserIdentity, SecurityBrowserSession } from "../api/types";
import { BrowserResearchSuite, type RepeaterDraftStore } from "./BrowserResearchSuite";
import { DialogProvider } from "./DialogSystem";
import { InterceptionTransport } from "./InterceptionTransport";

const session = { id: "s1", activeTabId: "t1", tabs: [{ id: "t1", url: "https://example.test/" }] } as SecurityBrowserSession;
const identity = { id: "i1", name: "Personal" } as SecurityBrowserIdentity;
const tabs = ["First", "Second"].map((name, i) => ({ id: `r${i}`, sessionId: "s1", identityId: "i1", revision: 1, name, method: "GET", url: `https://example.test/${i}`, headers: [], bodyTemplate: "", state: "ready" }));
const workspace = { siteNodes: [], crawlJobs: [], intercepts: [], attacks: [], attackResults: [], tokenAnalyses: [], repeaterTabs: tabs, repeaterResults: [{ id: "result", tabId: "r0", sequence: 0, statusCode: 200, durationMs: 12, responseBytes: 4, responseHeaders: [["Content-Type", "text/plain"]], createdAt: "2026-09-12T12:00:00Z" }] };
function client() { return { getSecurityBrowserResearch: vi.fn().mockResolvedValue(workspace), updateSecurityBrowserRepeaterTab: vi.fn().mockRejectedValue(new Error("Save failed; try again")) } as unknown as ApiClient; }
function element(api: ApiClient, store: RepeaterDraftStore = new Map(), selectedSession = session) {
  return <MemoryRouter><DialogProvider><BrowserResearchSuite api={api} desktop={false} identity={identity} operatorId="operator" projectId="p1" session={selectedSession} view="repeater" draftStore={store} /></DialogProvider></MemoryRouter>;
}
async function choose(name: string) { fireEvent.click(await screen.findByRole("button", { name: `GET ${name}` })); }
afterEach(() => vi.useRealTimers());

describe("manual request workspace presentation", () => {
  it("preserves independent unsaved drafts when switching requests", async () => {
    render(element(client()));
    await choose("First");
    fireEvent.change(screen.getByLabelText("Body"), { target: { value: "first draft" } });
    await choose("Second");
    fireEvent.change(screen.getByLabelText("Body"), { target: { value: "second draft" } });
    fireEvent.click(screen.getByRole("button", { name: /GET First/ }));
    expect(screen.getByLabelText("Body")).toHaveValue("first draft");
    expect(screen.getByText("Request · Unsaved changes")).toBeVisible();
  });
  it("restores selection and unsaved content after the tool unmounts", async () => {
    const store: RepeaterDraftStore = new Map(); const api = client();
    const first = render(element(api, store)); await choose("First");
    fireEvent.change(screen.getByLabelText("Body"), { target: { value: "keep me" } });
    first.unmount(); render(element(api, store));
    await screen.findByRole("heading", { name: "Repeater" });
    expect(screen.getByLabelText("Body")).toHaveValue("keep me");
    expect(screen.getByRole("button", { name: /GET First/ })).toHaveAttribute("aria-pressed", "true");
  });
  it("isolates every draft field when the session changes", async () => {
    const store: RepeaterDraftStore = new Map(); const api = client(); const view = render(element(api, store));
    await choose("First"); fireEvent.change(screen.getByLabelText("Body"), { target: { value: "private draft" } });
    view.rerender(element(api, store, { ...session, id: "s2", tabs: [{ ...session.tabs[0], url: "https://example.test/other" }] }));
    await screen.findByRole("heading", { name: "Repeater" });
    expect(screen.getByLabelText("Body")).toHaveValue("");
    expect(screen.getByLabelText("URL")).toHaveValue("https://example.test/other");
    view.rerender(element(api, store)); await screen.findByRole("heading", { name: "Repeater" });
    expect(screen.getByLabelText("Body")).toHaveValue("private draft");
  });
  it("shows the selected response directly and explains its provenance", async () => {
    render(element(client())); await choose("First");
    expect(screen.getByText("200", { exact: true })).toBeVisible();
    expect(screen.getByText("Content-Type: text/plain")).toBeVisible();
    expect(screen.getByText(/submitted request revision is unavailable/)).toBeVisible();
    expect(screen.getByText("No retained response body.")).toBeVisible();
    await choose("Second"); expect(screen.getByText("No response yet")).toBeVisible();
  });
  it("does not dismiss an action failure when a background read succeeds", async () => {
    const api = client(); render(element(api)); await choose("First");
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText(/Save failed; try again/)).toBeVisible();
    fireEvent.focus(window);
    await waitFor(() => expect(api.getSecurityBrowserResearch).toHaveBeenCalledTimes(2));
    expect(screen.getByText(/Save failed; try again/)).toBeVisible();
  });
  it("warns before refreshing an unsaved draft", async () => {
    render(element(client())); await choose("First");
    fireEvent.change(screen.getByLabelText("Body"), { target: { value: "keep me" } });
    const event = new Event("beforeunload", { cancelable: true });
    act(() => { window.dispatchEvent(event); });
    expect(event.defaultPrevented).toBe(true);
  });
  it("requires explicit discard and restores the saved fields", async () => {
    render(element(client())); await choose("First");
    fireEvent.change(screen.getByLabelText("Body"), { target: { value: "temporary" } });
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(Array.from(dialog.querySelectorAll("button")).find((button) => button.textContent === "Discard changes")!);
    await waitFor(() => expect(screen.getByLabelText("Body")).toHaveValue(""));
  });
  it("clears selected content after deleting the selected request", async () => {
    const api = client();
    Object.assign(api, { deleteSecurityBrowserRepeaterTab: vi.fn().mockResolvedValue(undefined) });
    render(element(api)); await choose("First");
    fireEvent.click(screen.getByRole("button", { name: "Delete Repeater request First" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(Array.from(dialog.querySelectorAll("button")).find((button) => button.textContent === "Delete request")!);
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("Repeater"));
    expect(screen.getByLabelText("URL")).toHaveValue("https://example.test/");
    expect(screen.getByText("No request selected")).toBeVisible();
  });
  it("discloses response preview truncation", async () => {
    const api = client();
    Object.assign(api, { getSecurityBrowserResearch: vi.fn().mockResolvedValue({ ...workspace, repeaterResults: [{ ...workspace.repeaterResults[0], responseBodyArtifactId: "body" }] }), getArtifactContent: vi.fn().mockResolvedValue({ text: async () => "x".repeat(1_048_577) }) });
    render(element(api)); await choose("First");
    fireEvent.click(screen.getByRole("button", { name: "Preview redacted body" }));
    expect(await screen.findByText(/Preview truncated at 1,048,576 characters/)).toBeVisible();
  });
  it("recovers from an initial read failure without hiding the retry", async () => {
    const api = client();
    Object.assign(api, { getSecurityBrowserResearch: vi.fn().mockRejectedValueOnce(new Error("Connection unavailable")).mockResolvedValue(workspace) });
    render(element(api));
    expect(await screen.findByText(/Connection unavailable/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await choose("First"); expect(screen.getByText("200", { exact: true })).toBeVisible();
  });
  it("exposes pause and resume as labeled controls and disables repeat submissions", () => {
    const onToggle = vi.fn(); const props = { available: true, desktop: true, pending: false, onToggle, onSetup: vi.fn() };
    const view = render(<InterceptionTransport {...props} enabled={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Pause requests" })); expect(onToggle).toHaveBeenCalledOnce();
    view.rerender(<InterceptionTransport {...props} enabled pending />);
    expect(screen.getByRole("button", { name: "Resume requests" })).toBeDisabled();
    expect(screen.getByText(/Interception on/)).toBeVisible();
  });
  it("provides an explicit setup path when interception is unavailable", () => {
    const onSetup = vi.fn(); render(<InterceptionTransport available={false} enabled={false} desktop pending={false} onToggle={vi.fn()} onSetup={onSetup} />);
    fireEvent.click(screen.getByRole("button", { name: "Set up interception" })); expect(onSetup).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Pause requests" })).not.toBeInTheDocument();
  });
});
