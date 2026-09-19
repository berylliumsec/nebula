/**
 * Helpers for the per-host ``ssh.<host>.run_command`` tools that Core builds
 * from enabled SSH environments (see ``nebula/v3/environments.py``).
 */

const SSH_TOOL = /^ssh\.([a-z0-9_-]+?)(?:-[0-9a-f]{6})?\.run_command$/;
// First line of the tool description, which Core also stores on approvals.
const SSH_DESCRIPTION = /^Run a shell command on the operator's machine (.+) \(SSH host (\S+)\)\.$/m;

/** Short host name from a tool name, or undefined for other tools. */
export function sshToolHost(toolName: string | undefined): string | undefined {
  return toolName ? SSH_TOOL.exec(toolName)?.[1] : undefined;
}

export interface SshApprovalTarget {
  label: string;
  alias: string;
  command: string;
  cwd?: string;
}

/** Host and command for an approval of an SSH tool call, if that is what it is. */
export function sshApprovalTarget(approval: Record<string, unknown>): SshApprovalTarget | undefined {
  const exact = approval.exact_request && typeof approval.exact_request === "object" ? approval.exact_request as Record<string, unknown> : approval;
  const toolName = typeof exact.tool_name === "string" ? exact.tool_name : undefined;
  if (!sshToolHost(toolName)) return undefined;
  const args = exact.arguments && typeof exact.arguments === "object" ? exact.arguments as Record<string, unknown> : {};
  const effects = Array.isArray(approval.expected_effects) ? approval.expected_effects : [];
  const match = effects.map((effect) => typeof effect === "string" ? SSH_DESCRIPTION.exec(effect) : null).find(Boolean);
  const host = sshToolHost(toolName)!;
  return {
    label: match?.[1] ?? host,
    alias: match?.[2] ?? host,
    command: typeof args.command === "string" ? args.command : "",
    cwd: typeof args.cwd === "string" && args.cwd ? args.cwd : undefined,
  };
}
