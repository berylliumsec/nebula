import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { Upload } from "lucide-react";
import { PageHeaderAction } from "./PageHeader";

afterEach(cleanup);

test("header action keeps its name independently of the responsive label", async () => {
  const activate = vi.fn();
  render(<PageHeaderAction label="Add document or script" icon={<Upload />} onClick={activate} />);
  const action = screen.getByRole("button", {name: "Add document or script"});
  expect(action).toHaveAttribute("title", "Add document or script");
  expect(action.querySelector(".page-header-action-icon")).toHaveAttribute("aria-hidden", "true");
  expect(action.querySelector(".page-header-action-label")).toHaveTextContent("Add document or script");
  await userEvent.tab();
  expect(action).toHaveFocus();
  await userEvent.keyboard("{Enter}");
  expect(activate).toHaveBeenCalledTimes(1);
});

test("header action retains a disabled explanation and prevents activation", async () => {
  const activate = vi.fn();
  render(<PageHeaderAction label="Add asset" icon={<Upload />} disabled title="Create a project first" onClick={activate} />);
  const action = screen.getByRole("button", {name: "Add asset"});
  expect(action).toBeDisabled();
  expect(action).toHaveAttribute("title", "Create a project first");
  await userEvent.click(action);
  expect(activate).not.toHaveBeenCalled();
});

test("header action updates its visible and accessible busy label together", () => {
  const {rerender} = render(<PageHeaderAction label="Upload file" icon={<Upload />} />);
  rerender(<PageHeaderAction label="Adding source…" icon={<Upload />} disabled />);
  const action = screen.getByRole("button", {name: "Adding source…"});
  expect(action).toHaveTextContent("Adding source…");
  expect(action).toBeDisabled();
  expect(screen.queryByRole("button", {name: "Upload file"})).not.toBeInTheDocument();
});
