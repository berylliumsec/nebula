import type { ProviderRequestInput } from "../api/types";

export function ProviderRequestInputDetails({ request }: { request: ProviderRequestInput }) {
  const parts = [
    ["Instructions and references", request.instructions],
    ["Conversation", request.conversation],
    ["Tool schemas", request.toolSchemas],
    ["Tool results", request.toolResults],
    ["Other", request.other],
  ] as const;

  // The provider's own count leads, beside the share it served from its prompt
  // cache. Usage without a cache figure records zero cached tokens, so zero is
  // indistinguishable from "not reported" and shows no share rather than 0%.
  const reported = request.reportedInputTokens;
  const cached = request.reportedCachedInputTokens;
  const meta = [
    reported === undefined ? undefined : `${reported.toLocaleString()} reported`,
    reported && cached ? `${Math.min(100, Math.round((cached / reported) * 100))}% cached` : undefined,
  ].filter(Boolean).join(" · ");

  return <details className="session-memory">
    <summary>Last provider request{meta && <span className="session-disclosure-meta"> · {meta}</span>}</summary>
    <div>
      <p>{request.estimatedTotal.toLocaleString()} estimated input tokens{request.reportedInputTokens === undefined ? "" : ` · ${request.reportedInputTokens.toLocaleString()} reported by provider`}</p>
      <ul>{parts.filter(([, tokens]) => tokens > 0).map(([label, tokens]) => <li key={label}>{label}: {tokens.toLocaleString()} estimated</li>)}</ul>
      <small>Estimates use a provider-neutral calculation; provider usage is authoritative. This is the last request of the reply{request.attempt > 1 ? ` (attempt ${request.attempt})` : ""}.</small>
    </div>
  </details>;
}
