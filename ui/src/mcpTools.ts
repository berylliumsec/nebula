/**
 * Readable identities for MCP tool calls.
 *
 * A tool call reaches the conversation under a runtime name, which is built to
 * be unique rather than to be read: Core's own broker names an MCP tool
 * ``mcp.<profile digest>.<tool>`` (see ``mcp_tool_runtime_name`` in
 * ``nebula/v3/mcp.py``) and the Claude harness names one ``mcp__<server>__<tool>``
 * (``_parse_claude_mcp_name`` in ``nebula/v3/harnesses.py``). Core now sends a
 * readable ``display_name`` with every call it brokers; these helpers recover
 * what they can for everything else, including conversations recorded before
 * that name existed.
 */

/** Core's broker names, whose digest distinguishes two servers sharing a tool name. */
const CORE_MCP_TOOL = /^mcp\.[0-9a-f]{12}\.(.+)$/;
// Kept in step with `_parse_claude_mcp_name`: a server name may hold single
// underscores, and the doubled underscore separates it from the tool.
const VENDOR_MCP_TOOL = /^mcp__([^_]+(?:_[^_]+)*)__(.+)$/;

/** Server and tool halves of an MCP tool name, or undefined for other tools. */
export function mcpToolIdentity(toolName: string | undefined): { server?: string; tool: string } | undefined {
  if (!toolName) return undefined;
  const vendor = VENDOR_MCP_TOOL.exec(toolName);
  if (vendor) return { server: vendor[1], tool: vendor[2] };
  const core = CORE_MCP_TOOL.exec(toolName);
  return core ? { tool: core[1] } : undefined;
}

/** How an MCP tool call is written wherever an operator reads one. */
export function formatMcpToolLabel(server: string | undefined, tool: string): string {
  return server ? `${server} · ${tool}` : tool;
}

/**
 * Readable label for an MCP tool name, or undefined when it is not one.
 *
 * The server half is only recoverable from a vendor name; Core's digest is not
 * reversible, so a brokered call without its `display_name` keeps the tool
 * alone rather than the digest.
 */
export function mcpToolLabel(toolName: string | undefined): string | undefined {
  const identity = mcpToolIdentity(toolName);
  return identity && formatMcpToolLabel(identity.server, identity.tool);
}

// Servers that are the harness itself rather than something the operator
// connected: prefixing their tools would name the harness twice.
const BUILT_IN_SERVERS = new Set(["claude", "codex", "grok", "nebula"]);

/** Whether a harness activity item's server is a connected MCP server. */
export function isConnectedMcpServer(serverId: string | undefined): boolean {
  return Boolean(serverId && !BUILT_IN_SERVERS.has(serverId.toLowerCase()));
}
