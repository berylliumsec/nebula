import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { OperatorBrowser } from "./OperatorBrowser";

class ResizeObserverStub {
  observe() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

function fixture(refuse = false) {
  const connection = { readyState: 1, send: vi.fn(), close: vi.fn(), onopen: undefined as (() => void) | undefined, onmessage: undefined as ((event: { data: string }) => void) | undefined };
  const request = vi.fn(async (path: string, options?: RequestInit) => {
    if (path.endsWith("?mode=operator")) return { session_id: "operator-1", active_tab_id: "tab-1", tabs: [{ id: "tab-1", url: "about:blank", title: "New tab" }] };
    if (path.endsWith("/operator-operations")) {
      const body = JSON.parse(String(options?.body));
      if (body.operation === "navigate") {
        if (refuse) throw new Error("The URL is outside this Project's browser scope.");
        return { url: body.url, title: "Example", text: "Page content", page_revision: "revision-1", captured_at: "2026-10-12T12:00:00Z" };
      }
      return { tabs: [{ id: "tab-1", url: "about:blank", title: "New tab" }] };
    }
    return {};
  });
  const api = { request, openOperatorBrowserStream: vi.fn(() => connection) } as unknown as ApiClient;
  render(<OperatorBrowser api={api} projectId="project-1" active onContext={vi.fn()} />);
  return { request, connection, api };
}

describe("OperatorBrowser", () => {
  it("opens a separate operator session and navigates from its own address field", async () => {
    const { request, connection, api } = fixture();
    await waitFor(() => expect(api.openOperatorBrowserStream).toHaveBeenCalledWith("operator-1", "tab-1"));
    act(() => { connection.onopen?.(); connection.onmessage?.({ data: JSON.stringify({ kind: "frame", data: "fixture" }) }); });
    expect(screen.getByRole("region", { name: "Interactive browser page" })).toContainElement(screen.getByRole("img", { name: /Live Chromium page/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Browser address" }), { target: { value: "example.test/page" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("browser-companion/operator-1/operator-operations", expect.objectContaining({
      body: JSON.stringify({ operation: "navigate", tab_id: "tab-1", url: "https://example.test/page" }),
    })));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Browser address" })).toHaveValue("https://example.test/page"));
  });

  it("reports a scope refusal beside the page and offers reconnect", async () => {
    const { api, connection } = fixture(true);
    await waitFor(() => expect(api.openOperatorBrowserStream).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("textbox", { name: "Browser address" }), { target: { value: "blocked.test" } });
    fireEvent.click(screen.getByRole("button", { name: "Go" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("outside this Project's browser scope");
    act(() => connection.onopen?.());
    expect(screen.getByRole("alert")).toHaveTextContent("outside this Project's browser scope");
    expect(screen.getByRole("button", { name: "Reconnect" })).toBeVisible();
  });
});
