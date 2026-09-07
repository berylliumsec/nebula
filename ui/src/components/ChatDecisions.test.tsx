import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ChatDecisions } from "./ChatDecisions";

test("seeded decision editing survives strict lifecycle replay and saves edited text", async () => {
  const calls: unknown[] = [];
  const api = {request: vi.fn(async (_path: string, options?: RequestInit) => {if (options?.body) calls.push(JSON.parse(String(options.body))); return [];})} as unknown as ApiClient;
  const seed = {messageId: "m", text: "Hello", selection: "Hello"};
  const {rerender} = render(<StrictMode><ChatDecisions api={api} sessionId="s" seed={seed} onSeedConsumed={() => undefined} onMessage={() => undefined} /></StrictMode>);
  fireEvent.change(screen.getByRole("textbox", {name: "Operator context text"}), {target: {value: "Keep concise"}});
  rerender(<StrictMode><ChatDecisions api={api} sessionId="s" onSeedConsumed={() => undefined} onMessage={() => undefined} /></StrictMode>);
  expect(screen.getByRole("textbox", {name: "Operator context text"})).toHaveValue("Keep concise");
  fireEvent.click(screen.getByRole("button", {name: "Save operator context"}));
  await waitFor(() => expect(calls).toContainEqual(expect.objectContaining({text: "Keep concise", source_selection: "Hello", expected_revision: 0})));
});
