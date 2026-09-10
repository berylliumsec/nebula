export function AssistantApprovalDetails({request}: {request: Record<string, unknown>}) {
  const exact = request.exact_request && typeof request.exact_request === "object" ? request.exact_request as Record<string, unknown> : request;
  const args = exact.arguments && typeof exact.arguments === "object" ? exact.arguments as Record<string, unknown> : {};
  const resource = args.path ?? args.url ?? args.filename;
  return <><p>{String(exact.tool_name ?? exact.name ?? "Requested action")}</p>{typeof resource === "string" && <p className="chat-approval-resource" title={resource}>Resource: {resource}</p>}<p>Approving allows this action to run with the arguments below.</p><details><summary>Exact request</summary><pre tabIndex={0} role="region" aria-label="Exact request JSON">{JSON.stringify(exact, null, 2)}</pre></details></>;
}
