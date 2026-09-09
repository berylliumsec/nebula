import { fireEvent, render, screen, waitFor, act } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ApplicationModelReset } from "./ApplicationModelReset";

const counts = { revision: 7, objects: 11, relationships: 16, captures: 42, custom_definitions: 0 };
function setup(request = vi.fn().mockResolvedValue(counts)) {
  const onReset = vi.fn();
  const rendered = render(<ApplicationModelReset base="/model" projectName="Selected project" disabled={false} request={request} onReset={onReset} />);
  fireEvent.click(screen.getByRole("button", { name: "Start over" }));
  return { request, onReset, ...rendered };
}

it("shows scoped counts and defaults focus to Cancel without clearing", async () => {
  const { request } = setup();
  await screen.findByText("11 objects · 16 relationships · 42 captures");
  await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus());
  expect(screen.getByText(/Other evidence, findings, chats, files, tabs and sign-ins stay/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(1);
});

it("retries an uncertain response with exactly the same request", async () => {
  const request = vi.fn().mockResolvedValueOnce(counts).mockRejectedValueOnce(Error("offline")).mockResolvedValueOnce({ revision: 8 });
  const { onReset } = setup(request);
  await screen.findByText("11 objects · 16 relationships · 42 captures");
  fireEvent.click(screen.getByRole("button", { name: "Clear and start over" }));
  fireEvent.click(await screen.findByRole("button", { name: "Retry clear" }));
  await waitFor(() => expect(onReset).toHaveBeenCalledTimes(1));
  expect(request.mock.calls[1]).toEqual(request.mock.calls[2]);
  expect(JSON.parse(request.mock.calls[1][1].body)).toMatchObject({ expected_revision: 7, confirmation: "clear-model-and-browser-captures" });
});

it("requires a new preview after a concurrent edit", async () => {
  const request = vi.fn().mockResolvedValueOnce(counts).mockRejectedValueOnce({ status: 409 }).mockResolvedValueOnce({ ...counts, revision: 8 }).mockResolvedValueOnce({ revision: 9 });
  const { onReset } = setup(request);
  await screen.findByText("11 objects · 16 relationships · 42 captures");
  fireEvent.click(screen.getByRole("button", { name: "Clear and start over" }));
  fireEvent.click(await screen.findByRole("button", { name: "Review latest counts" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Clear and start over" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Clear and start over" }));
  await waitFor(() => expect(onReset).toHaveBeenCalledTimes(1));
  expect(JSON.parse(request.mock.calls[3][1].body).expected_revision).toBe(8);
  expect(JSON.parse(request.mock.calls[3][1].body).idempotency_key).not.toBe(JSON.parse(request.mock.calls[1][1].body).idempotency_key);
});

it("can recover from a failed preview", async () => {
  const request = vi.fn().mockRejectedValueOnce(Error("offline")).mockResolvedValueOnce(counts);
  setup(request);
  expect(await screen.findByRole("alert")).toHaveTextContent("Nothing has been cleared");
  fireEvent.click(screen.getByRole("button", { name: "Review latest counts" }));
  await screen.findByText("11 objects · 16 relationships · 42 captures");
});

it("ignores a late preview after cancel", async () => {
  let resolve!: (value: unknown) => void;
  setup(vi.fn(() => new Promise(r => { resolve = r; })));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await act(async () => resolve(counts));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("blocks duplicate submits and ignores a completed reset after changing projects", async () => {
  let resolve!: (value: unknown) => void;
  const request = vi.fn().mockResolvedValueOnce(counts).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  const { onReset, unmount } = setup(request);
  await screen.findByText("11 objects · 16 relationships · 42 captures");
  const clear = screen.getByRole("button", { name: "Clear and start over" });
  fireEvent.click(clear); fireEvent.click(clear);
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  expect(request).toHaveBeenCalledTimes(2);
  unmount();
  await act(async () => resolve({ revision: 8 }));
  expect(onReset).not.toHaveBeenCalled();
});
