import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {ButlerCheckIntervalSettings} from "./ButlerCheckIntervalSettings";

describe("ButlerCheckIntervalSettings", () => {
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
});
