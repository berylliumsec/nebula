import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { ChatSearchPanel } from "./ChatSearchPanel";

async function openSearch() {
  await userEvent.click(screen.getByTitle("Search messages and bookmarks"));
}

test("search keeps named filters and submits their selected values with Enter", async () => {
  const search = vi.fn().mockResolvedValue({items: [], next_offset: null});
  render(<ChatSearchPanel search={search} onSelect={vi.fn()} />);
  await openSearch();
  expect(screen.getByRole("checkbox", {name: "This conversation"})).toBeChecked();
  await userEvent.click(screen.getByRole("checkbox", {name: "This conversation"}));
  await userEvent.click(screen.getByRole("checkbox", {name: "Bookmarks only"}));
  await userEvent.type(screen.getByRole("searchbox", {name: "Search transcript"}), "saved note{Enter}");
  await waitFor(() => expect(search).toHaveBeenCalledWith("saved note", true, false, 0));
  expect(await screen.findByText("No matching messages.")).toBeVisible();
});

test("search explains a failure and allows retry from the same query", async () => {
  const search = vi.fn().mockRejectedValueOnce(new Error("Search unavailable. Try again.")).mockResolvedValueOnce({items: [], next_offset: null});
  render(<ChatSearchPanel search={search} onSelect={vi.fn()} />);
  await openSearch();
  await userEvent.type(screen.getByRole("searchbox", {name: "Search transcript"}), "retry");
  await userEvent.click(screen.getByRole("button", {name: "Search messages"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Search unavailable");
  await userEvent.click(screen.getByRole("button", {name: "Search messages"}));
  expect(await screen.findByText("No matching messages.")).toBeVisible();
  expect(search).toHaveBeenLastCalledWith("retry", false, true, 0);
});

test("search disables duplicate submission while busy and selects the returned hit", async () => {
  const hit = {message_id: "m1", session_id: "s1", title: "Saved conversation", role: "assistant", excerpt: "Found message", sequence: 1};
  let finish!: (result: {items: typeof hit[]; next_offset: null}) => void;
  const search = vi.fn(() => new Promise<{items: typeof hit[]; next_offset: null}>(resolve => {finish = resolve;}));
  const select = vi.fn();
  render(<ChatSearchPanel search={search} onSelect={select} />);
  await openSearch();
  await userEvent.click(screen.getByRole("button", {name: "Search messages"}));
  expect(screen.getByRole("button", {name: "Searching…"})).toBeDisabled();
  finish({items: [hit], next_offset: null});
  await userEvent.click(await screen.findByRole("button", {name: /Saved conversation/}));
  expect(select).toHaveBeenCalledWith(hit);
});
