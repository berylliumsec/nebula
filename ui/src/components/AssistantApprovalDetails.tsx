import { sshApprovalTarget } from "../sshTools";

export function AssistantApprovalDetails({request}: {request: Record<string, unknown>}) {
  const ssh = sshApprovalTarget(request);
  if (ssh) return <><p className="chat-approval-host">Run on <strong>{ssh.label}</strong> <code>{ssh.alias}</code></p><pre className="chat-approval-command" tabIndex={0} role="region" aria-label="Command">{ssh.command}</pre>{ssh.cwd && <p className="chat-approval-resource" title={ssh.cwd}>In {ssh.cwd}</p>}<p>Approving runs this command once over SSH on that machine.</p></>;
  const exact = request.exact_request && typeof request.exact_request === "object" ? request.exact_request as Record<string, unknown> : request;
  const args = exact.arguments && typeof exact.arguments === "object" ? exact.arguments as Record<string, unknown> : {};
  const resource = args.path ?? args.url ?? args.filename;
  return <><p>{String(exact.tool_name ?? exact.name ?? "Requested action")}</p>{typeof resource === "string" && <p className="chat-approval-resource" title={resource}>Resource: {resource}</p>}<p>Approving allows this action to run with the arguments below.</p><details><summary>Exact request</summary><pre tabIndex={0} role="region" aria-label="Exact request JSON">{JSON.stringify(exact, null, 2)}</pre></details></>;
}
