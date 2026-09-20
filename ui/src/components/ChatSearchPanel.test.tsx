import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { ChatSearchPanel } from "./ChatSearchPanel";

async function openSearch() {
  expect(screen.getByRole("searchbox", {name: "Search transcript"})).toHaveFocus();
}

test("search keeps named filters and submits their selected values with Enter", async () => {
  const search = vi.fn().mockResolvedValue({items: [], next_offset: null});
  render(<ChatSearchPanel open onClose={vi.fn()} search={search} onSelect={vi.fn()} />);
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
  render(<ChatSearchPanel open onClose={vi.fn()} search={search} onSelect={vi.fn()} />);
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
  render(<ChatSearchPanel open onClose={vi.fn()} search={search} onSelect={select} />);
  await openSearch();
  await userEvent.click(screen.getByRole("button", {name: "Search messages"}));
  expect(screen.getByRole("button", {name: "Searching…"})).toBeDisabled();
  finish({items: [hit], next_offset: null});
  await userEvent.click(await screen.findByRole("button", {name: /Saved conversation/}));
  expect(select).toHaveBeenCalledWith(hit);
});

 test("closed search reserves no row and reopening focuses the retained query", async () => {
  const search = vi.fn();
  const close = vi.fn();
  const {rerender, container} = render(<ChatSearchPanel open={false} onClose={close} search={search} onSelect={vi.fn()} />);
  expect(container).toBeEmptyDOMElement();
  rerender(<ChatSearchPanel open onClose={close} search={search} onSelect={vi.fn()} />);
  await userEvent.type(screen.getByRole("searchbox"), "retained query{Escape}");
  expect(close).toHaveBeenCalledOnce();
  await userEvent.click(screen.getByRole("button", {name: "Close transcript search"}));
  expect(close).toHaveBeenCalledTimes(2);
  rerender(<ChatSearchPanel open={false} onClose={close} search={search} onSelect={vi.fn()} />);
  expect(container).toBeEmptyDOMElement();
  rerender(<ChatSearchPanel open onClose={close} search={search} onSelect={vi.fn()} />);
  expect(screen.getByRole("searchbox")).toHaveValue("retained query");
  expect(screen.getByRole("searchbox")).toHaveFocus();
});

test("More matches extends the result list and keeps the current match", async () => {
  const hit = (sequence: number) => ({message_id: `m${sequence}`, session_id: "s1", title: `Hit ${sequence}`, role: "assistant", excerpt: `Excerpt ${sequence}`, sequence});
  const search = vi.fn()
    .mockResolvedValueOnce({items: [hit(1), hit(2)], next_offset: 2})
    .mockResolvedValueOnce({items: [hit(3), hit(4)], next_offset: null});
  const select = vi.fn();
  render(<ChatSearchPanel open onClose={vi.fn()} search={search} onSelect={select} />);
  await openSearch();
  await userEvent.type(screen.getByRole("searchbox", {name: "Search transcript"}), "hit{Enter}");
  await userEvent.click(await screen.findByRole("button", {name: "Next match"}));
  expect(select).toHaveBeenLastCalledWith(hit(2));

  await userEvent.click(screen.getByRole("button", {name: "More matches"}));
  await waitFor(() => expect(search).toHaveBeenLastCalledWith("hit", false, true, 2));
  expect(await screen.findByRole("button", {name: /Hit 4/})).toBeVisible();
  expect(screen.getAllByRole("listitem")).toHaveLength(4);
  expect(screen.getByRole("button", {name: /Hit 2/})).toHaveAttribute("aria-current", "true");
  expect(screen.queryByRole("button", {name: "More matches"})).not.toBeInTheDocument();
});
