import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { announceSettingsSaved, SettingsSaveFeedback } from "./SettingsSaveFeedback";

describe("SettingsSaveFeedback", () => {
  afterEach(() => vi.useRealTimers());

  it("announces a successful save and then clears the confirmation", () => {
    vi.useFakeTimers();
    render(<SettingsSaveFeedback />);

    act(() => announceSettingsSaved("Runner profile verified and updated."));
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
    expect(screen.getByRole("status")).toHaveTextContent("Runner profile verified and updated.");

    act(() => vi.advanceTimersByTime(2800));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
