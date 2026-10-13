import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { DialogProvider } from "./components/DialogSystem";
import { ThemeProvider } from "./state/ThemeContext";
import { WorkspaceProvider } from "./state/WorkspaceContext";

vi.mock("./components/CodeMirrorSurface", () => ({
  CodeMirrorSurface: () => <textarea aria-label="Code editor" />,
}));

afterEach(() => {
  vi.unstubAllGlobals();
  delete (Element.prototype as Element & { scrollIntoView?: unknown }).scrollIntoView;
  localStorage.clear();
  sessionStorage.clear();
});

it("offers one mobile New chat action and routes the keyboard shortcut to it", async () => {
  Object.defineProperty(Element.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
    matches: query.includes("max-width: 760px"), media: query, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  })));
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const pathname = new URL(String(input)).pathname;
    if (pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable" }), { status: 200 });
    if (pathname.endsWith("/engagements")) return new Response(JSON.stringify([{
      id: "engagement-1", name: "Live engagement", status: "active", revision: 1,
      created_at: "2026-10-13T00:00:00Z", updated_at: "2026-10-13T00:00:00Z", metadata: {},
    }]), { status: 200 });
    if (pathname.endsWith("/pending-turn")) return new Response("null", { status: 200 });
    return new Response("[]", { status: 200 });
  }));

  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/?view=chat"]}><ThemeProvider><WorkspaceProvider><DialogProvider><App /></DialogProvider></WorkspaceProvider></ThemeProvider></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole("button", { name: "New chat" })).toBeEnabled());
  expect(screen.getAllByRole("button", { name: "New chat" })).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "New chat" }));
  const composer = await screen.findByRole("textbox", { name: "Message the analyst assistant" });
  await user.type(composer, "Unsent mobile draft");
  await user.keyboard("{Control>}n{/Control}");
  expect(composer).toHaveValue("");
});
