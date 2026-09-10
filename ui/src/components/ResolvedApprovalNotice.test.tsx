import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ResolvedApprovalNotice } from "./ResolvedApprovalNotice";

it("explains a recorded decision and only invokes explicit recovery actions", () => {
  const onCheck = vi.fn(), onStop = vi.fn();
  render(<ResolvedApprovalNotice status="approved" busy={false} canStop onCheck={onCheck} onStop={onStop} />);
  expect(screen.getByRole("status")).toHaveTextContent("Decision recorded: approved");
  expect(onCheck).not.toHaveBeenCalled(); expect(onStop).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Check response status" }));
  expect(onCheck).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Stop waiting" }));
  expect(onStop).toHaveBeenCalledTimes(1);
});

it("disables in-flight recovery and hides unsupported stop", () => {
  render(<ResolvedApprovalNotice status="rejected" busy canStop={false} onCheck={() => {}} onStop={() => {}} />);
  expect(screen.getByRole("button", { name: "Check response status" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Stop waiting" })).toBeNull();
});
