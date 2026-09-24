import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CallbackWaitingStatus } from "./CallbackWaitingStatus";

const resultsUrl = "http://192.168.1.155:8000/api/v1/automation-processes/798fd1d5/results";

describe("CallbackWaitingStatus", () => {
  const writeText = vi.fn();

  beforeEach(() => {
    writeText.mockReset();
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  });

  it("keeps setup detail collapsed while exposing the exact copy action", async () => {
    writeText.mockResolvedValue(undefined);
    render(<CallbackWaitingStatus summary="Waiting for results" resultsUrl={resultsUrl} />);

    expect(screen.getByText("Callback ready")).toBeInTheDocument();
    expect(screen.getByText("How this callback works").closest("details")).not.toHaveAttribute("open");

    fireEvent.click(screen.getByRole("button", { name: "Copy results URL" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(resultsUrl));
    expect(screen.getByText("Copied")).toBeInTheDocument();
  });

  it("labels a subagent wait as one, without callback copy", () => {
    render(<CallbackWaitingStatus summary="Waiting for 2 subagents to report." kind="subagents" />);

    const status = screen.getByRole("status", { name: "Waiting for subagents" });
    expect(status).toHaveTextContent("Waiting for 2 subagents to report.");
    expect(screen.queryByText("Callback ready")).toBeNull();
    expect(screen.queryByRole("status", { name: "Waiting for command results" })).toBeNull();
  });

  it("leaves the URL selectable and explains clipboard failure", async () => {
    writeText.mockRejectedValue(new Error("denied"));
    render(<CallbackWaitingStatus summary="Waiting for results" resultsUrl={resultsUrl} />);

    fireEvent.click(screen.getByRole("button", { name: "Copy results URL" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Select and copy the URL instead");
    expect(screen.getByTitle(resultsUrl)).toHaveTextContent(resultsUrl);
  });
});
