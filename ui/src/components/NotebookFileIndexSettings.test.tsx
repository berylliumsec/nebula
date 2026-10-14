import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {DialogProvider} from "./DialogSystem";
import {NotebookFileIndexSettings} from "./NotebookFileIndexSettings";

it("connects a browsed host index and removes it", async () => {
  const api = {
    notebookFileIndex: vi.fn(async () => ({path: null, state: "none", detail: null})),
    setNotebookFileIndex: vi.fn(async (_project: string, path: string) => ({path, state: "connected", detail: null})),
    removeNotebookFileIndex: vi.fn(async () => ({path: null, state: "none", detail: null})),
    listHostWorkspaceFolders: vi.fn(async () => ({path: "/indexes", parent: "/", directories: [], files: [{name: "inventory.db", path: "/indexes/inventory.db"}], truncated: false})),
  };
  const changed = vi.fn();
  const user = userEvent.setup();
  render(<DialogProvider><NotebookFileIndexSettings api={api as unknown as ApiClient} projectId="p" onChanged={changed} /></DialogProvider>);
  await user.click(screen.getByRole("button", {name: "Browse index files"}));
  await user.click(await screen.findByRole("button", {name: "inventory.db"}));
  await waitFor(() => expect(api.setNotebookFileIndex).toHaveBeenCalledWith("p", "/indexes/inventory.db"));
  expect(await screen.findByText("File index connected")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Remove index"}));
  await waitFor(() => expect(api.removeNotebookFileIndex).toHaveBeenCalledWith("p"));
  expect(changed).toHaveBeenCalledTimes(2);
});

it("loads SQLite files on later host folder pages", async () => {
  const api = {
    notebookFileIndex: vi.fn(async () => ({path: null, state: "none", detail: null})),
    setNotebookFileIndex: vi.fn(async (_project: string, path: string) => ({path, state: "connected", detail: null})),
    listHostWorkspaceFolders: vi.fn(async (_path: string | undefined, offset: number) => offset
      ? {path: "/indexes", parent: "/", directories: [], files: [{name: "later.db", path: "/indexes/later.db"}], truncated: false}
      : {path: "/indexes", parent: "/", directories: [{name: "first", path: "/indexes/first"}], files: [], truncated: true, nextOffset: 1}),
  };
  const user = userEvent.setup();
  render(<DialogProvider><NotebookFileIndexSettings api={api as unknown as ApiClient} projectId="p" onChanged={vi.fn()} /></DialogProvider>);
  await user.click(screen.getByRole("button", {name: "Browse index files"}));
  await user.click(await screen.findByRole("button", {name: "Load more index entries"}));
  await user.click(await screen.findByRole("button", {name: "later.db"}));
  expect(api.setNotebookFileIndex).toHaveBeenCalledWith("p", "/indexes/later.db");
});
