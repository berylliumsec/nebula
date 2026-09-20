import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AssistantApprovalDetails } from "./AssistantApprovalDetails";

describe("assistant approval details", () => {
  it("names an MCP tool by the server and tool Core recorded", () => {
    render(<AssistantApprovalDetails request={{
      exact_request: {
        tool_name: "mcp.9a4c1f0b77de.create_issue",
        display_name: "GitHub · create_issue",
        arguments: { title: "Broken link" },
      },
    }} />);
    expect(screen.getByText("GitHub · create_issue")).toBeTruthy();
  });

  it("falls back to the tool half when an older approval carries only the runtime name", () => {
    render(<AssistantApprovalDetails request={{
      exact_request: { tool_name: "mcp.9a4c1f0b77de.create_issue", arguments: {} },
    }} />);
    expect(screen.getByText("create_issue")).toBeTruthy();
  });

  it("leaves a non-MCP tool named as it is", () => {
    render(<AssistantApprovalDetails request={{
      exact_request: { tool_name: "run_command", arguments: { command: "ls" } },
    }} />);
    expect(screen.getByText("run_command")).toBeTruthy();
  });
});
