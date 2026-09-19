import { cleanup, render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useNavigationChords } from "./useNavigationChords";

afterEach(cleanup);

function Harness({ navigate }: { navigate: (path: string) => void }) {
  useNavigationChords(navigate);
  return <><input aria-label="Notes" /><button type="button">Page</button></>;
}

describe("navigation chords", () => {
  it("navigates on G then a page letter and ignores typing", async () => {
    const navigate = vi.fn();
    const user = userEvent.setup();
    const view = render(<Harness navigate={navigate} />);
    await user.click(view.getByRole("button", { name: "Page" }));
    await user.keyboard("gf");
    expect(navigate).toHaveBeenLastCalledWith("/findings");
    await user.keyboard("g,");
    expect(navigate).toHaveBeenLastCalledWith("/settings");
    await user.keyboard("gz");
    expect(navigate).toHaveBeenCalledTimes(2);

    await user.click(view.getByRole("textbox", { name: "Notes" }));
    await user.keyboard("go");
    expect(navigate).toHaveBeenCalledTimes(2);
  });
});
