import {act, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {ButlerCheckIntervalSettings} from "./ButlerCheckIntervalSettings";
import {BUTLER_INTERVAL_CHANGED_EVENT} from "../api/butlerInstructions";

describe("ButlerCheckIntervalSettings", () => {
  it("refreshes a mounted unchanged form after Butler saves the interval", async () => {
    const initial = {check_interval_minutes: 5, revision: 0, last_check_at: null, next_check_at: null};
    const latest = {...initial, check_interval_minutes: 2, revision: 1};
    const request = vi.fn().mockResolvedValueOnce(initial).mockResolvedValueOnce(latest);
    render(<ButlerCheckIntervalSettings api={{request} as unknown as ApiClient} />);
    const interval = screen.getByRole("spinbutton", {name: "Check every"});
    await waitFor(() => expect(interval).toBeEnabled());
    act(() => window.dispatchEvent(new Event(BUTLER_INTERVAL_CHANGED_EVENT)));
    await waitFor(() => expect(interval).toHaveValue(2));
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("keeps an unsaved draft and requires reload after an external save", async () => {
    const user = userEvent.setup();
    const initial = {check_interval_minutes: 5, revision: 0, last_check_at: null, next_check_at: null};
    const latest = {...initial, check_interval_minutes: 2, revision: 1};
    const request = vi.fn().mockResolvedValueOnce(initial).mockResolvedValueOnce(latest);
    render(<ButlerCheckIntervalSettings api={{request} as unknown as ApiClient} />);
    const interval = screen.getByRole("spinbutton", {name: "Check every"});
    await waitFor(() => expect(interval).toBeEnabled());
    await user.clear(interval);
    await user.type(interval, "7");
    act(() => window.dispatchEvent(new Event(BUTLER_INTERVAL_CHANGED_EVENT)));
    expect(interval).toHaveValue(7);
    expect(screen.getByRole("alert")).toHaveTextContent("Your draft is kept");
    expect(screen.getByRole("button", {name: "Save interval"})).toBeDisabled();
    expect(request).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", {name: "Reload"}));
    await waitFor(() => expect(interval).toHaveValue(2));
  });

  it("saves a custom interval and confirms Core readback", async () => {
    const user = userEvent.setup();
    const initial = {check_interval_minutes: 5, revision: 0, last_check_at: null, next_check_at: null};
    const updated = {check_interval_minutes: 2, revision: 1, last_check_at: null, next_check_at: null};
    const request = vi.fn()
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(updated)
      .mockResolvedValueOnce(updated);
    render(<ButlerCheckIntervalSettings api={{request} as unknown as ApiClient} />);
    const interval = await screen.findByRole("spinbutton", {name: "Check every"});
    expect(interval).toHaveValue(5);
    await user.clear(interval);
    await user.type(interval, "2");
    await user.click(screen.getByRole("button", {name: "Save interval"}));
    await waitFor(() => expect(request).toHaveBeenNthCalledWith(2, "butler/check-interval", {
      method: "PUT", body: JSON.stringify({check_interval_minutes: 2, expected_revision: 0}),
    }));
    expect(await screen.findByText("Saved. Butler checks every 2 minutes.")).toBeVisible();
    expect(interval).toHaveValue(2);
  });

  it("keeps a draft after a revision conflict and reloads the saved value", async () => {
    const user = userEvent.setup();
    const initial = {check_interval_minutes: 5, revision: 1, last_check_at: null, next_check_at: null};
    const latest = {...initial, check_interval_minutes: 10, revision: 2};
    const request = vi.fn()
      .mockResolvedValueOnce(initial)
      .mockRejectedValueOnce(new Error("409 conflict"))
      .mockResolvedValueOnce(latest);
    render(<ButlerCheckIntervalSettings api={{request} as unknown as ApiClient} />);
    const interval = await screen.findByRole("spinbutton", {name: "Check every"});
    await user.clear(interval);
    await user.type(interval, "2");
    await user.click(screen.getByRole("button", {name: "Save interval"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("changed elsewhere");
    expect(interval).toHaveValue(2);
    await user.click(screen.getByRole("button", {name: "Reload"}));
    await waitFor(() => expect(interval).toHaveValue(10));
  });

  it("saves the Butler chat that receives check summaries and confirms Core readback", async () => {
    const user = userEvent.setup();
    const options = [
      {project_id: "project-a", project_name: "Alpha", session_id: "butler-a", title: "Alpha Butler"},
      {project_id: "project-b", project_name: "Beta", session_id: "butler-b", title: "Beta Butler"},
    ];
    const target = {
      configured: null,
      effective: {source: "global_anchor", project_id: "project-a", session_id: "butler-a", title: "Alpha Butler", url: "/projects/project-a/workbench?view=chat&session=butler-a"},
      unavailable: [],
      last_fallback: {case_id: "case-1", fallbacks: [{source: "configured", project_id: "project-c", reason: "chat is archived"}], at: "2026-11-25T10:00:00Z"},
      pending: 0,
      options,
    };
    const initial = {check_interval_minutes: 5, revision: 3, last_check_at: null, next_check_at: null, summary_target: target};
    const updated = {
      ...initial,
      revision: 4,
      summary_target: {
        ...target,
        configured: {project_id: "project-b", session_id: "butler-b"},
        effective: {source: "configured", project_id: "project-b", session_id: "butler-b", title: "Beta Butler", url: "/projects/project-b/workbench?view=chat&session=butler-b"},
      },
    };
    const request = vi.fn()
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(updated)
      .mockResolvedValueOnce(updated);
    render(<ButlerCheckIntervalSettings api={{request} as unknown as ApiClient} />);
    const select = await screen.findByRole("combobox", {name: "Post check summaries to"});
    await waitFor(() => expect(select).toBeEnabled());
    expect(screen.getByRole("link", {name: "Alpha Butler"})).toHaveAttribute("href", "/projects/project-a/workbench?view=chat&session=butler-a");
    expect(screen.getByText(/chat is archived/)).toBeVisible();
    expect(screen.getByRole("button", {name: "Save summary chat"})).toBeDisabled();
    await user.selectOptions(select, "project-b/butler-b");
    await user.click(screen.getByRole("button", {name: "Save summary chat"}));
    await waitFor(() => expect(request).toHaveBeenNthCalledWith(2, "butler/check-interval", {
      method: "PUT", body: JSON.stringify({summary_target: {project_id: "project-b", session_id: "butler-b"}, expected_revision: 3}),
    }));
    expect(await screen.findByText("Saved. Check summaries post to Beta Butler in Beta.")).toBeVisible();
    expect(screen.getByRole("link", {name: "Beta Butler"})).toBeVisible();
  });
});
