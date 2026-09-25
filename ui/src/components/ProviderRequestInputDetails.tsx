import type { ProviderRequestInput } from "../api/types";

export function ProviderRequestInputDetails({ request }: { request: ProviderRequestInput }) {
  const parts = [
    ["Instructions and references", request.instructions],
    ["Conversation", request.conversation],
    ["Tool schemas", request.toolSchemas],
    ["Tool results", request.toolResults],
    ["Other", request.other],
  ] as const;

  return <details className="session-memory">
    <summary>Last provider request</summary>
    <div>
      <p>{request.estimatedTotal.toLocaleString()} estimated input tokens{request.reportedInputTokens === undefined ? "" : ` · ${request.reportedInputTokens.toLocaleString()} reported by provider`}</p>
      <ul>{parts.filter(([, tokens]) => tokens > 0).map(([label, tokens]) => <li key={label}>{label}: {tokens.toLocaleString()} estimated</li>)}</ul>
      <small>Estimates use a provider-neutral calculation; provider usage is authoritative. This is the last request of the reply{request.attempt > 1 ? ` (attempt ${request.attempt})` : ""}.</small>
    </div>
  </details>;
}
