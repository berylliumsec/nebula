import { describe, expect, it } from "vitest";
import { formatMcpToolLabel, isConnectedMcpServer, mcpToolIdentity, mcpToolLabel } from "./mcpTools";

describe("MCP tool identities", () => {
  it("splits a harness vendor name into its server and tool", () => {
    expect(mcpToolIdentity("mcp__design_system__get_design_context"))
      .toEqual({ server: "design_system", tool: "get_design_context" });
    expect(mcpToolLabel("mcp__design_system__get_design_context"))
      .toBe("design_system · get_design_context");
  });

  it("drops Core's profile digest, which names no server an operator knows", () => {
    expect(mcpToolIdentity("mcp.9a4c1f0b77de.create_issue")).toEqual({ tool: "create_issue" });
    expect(mcpToolLabel("mcp.9a4c1f0b77de.create_issue")).toBe("create_issue");
  });

  it("leaves tools that are not MCP calls alone", () => {
    expect(mcpToolLabel("run_command")).toBeUndefined();
    expect(mcpToolLabel("mcp.short.tool")).toBeUndefined();
    expect(mcpToolLabel(undefined)).toBeUndefined();
  });

  it("writes a server and tool the one way every surface shows them", () => {
    expect(formatMcpToolLabel("GitHub", "create_issue")).toBe("GitHub · create_issue");
    expect(formatMcpToolLabel(undefined, "create_issue")).toBe("create_issue");
  });

  it("treats a harness's own tool namespace as not connected", () => {
    expect(isConnectedMcpServer("claude")).toBe(false);
    expect(isConnectedMcpServer("nebula")).toBe(false);
    expect(isConnectedMcpServer("Codex")).toBe(false);
    expect(isConnectedMcpServer(undefined)).toBe(false);
    expect(isConnectedMcpServer("design_system")).toBe(true);
  });
});
