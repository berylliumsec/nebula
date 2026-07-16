import { ShieldAlert } from "lucide-react";
import { diagnosticErrorPresentation } from "./logger";

const referencePattern = /\s*Reference:\s*((?:err|req)_[A-Za-z0-9._:-]+?)\.?\s*$/i;

function stringField(value: Record<string, unknown> | undefined, ...names: string[]): string | undefined {
  for (const name of names) {
    const candidate = value?.[name];
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return undefined;
}

function boundedReason(value: string, fallback: string): string {
  const bounded = value
    .replaceAll("\0", "�")
    .replace(referencePattern, "")
    .trim();
  const result = bounded || fallback;
  return result.length <= 500 ? result : `${result.slice(0, 499)}…`;
}

export interface DiagnosticErrorNoticeProps {
  error: unknown;
  fallback?: string;
  title?: string;
  className?: string;
  compact?: boolean;
}

export function DiagnosticErrorNotice({
  error,
  fallback = "The operation could not be completed.",
  title,
  className,
  compact = false,
}: DiagnosticErrorNoticeProps) {
  const value = error && typeof error === "object" ? error as Record<string, unknown> : undefined;
  const rawMessage = error instanceof Error
    ? error.message
    : typeof error === "string"
      ? error
      : stringField(value, "message", "detail") ?? fallback;
  const messageReference = rawMessage.match(referencePattern)?.[1];
  const errorReference = stringField(value, "errorId", "error_id");
  const requestReference = stringField(value, "requestId", "request_id");
  const reference = errorReference ?? messageReference ?? requestReference;
  const remembered = diagnosticErrorPresentation(reference);
  const retryable = typeof value?.retryable === "boolean" ? value.retryable : remembered?.retryable;
  const code = stringField(value, "code") ?? remembered?.code;
  const operatorDetail = stringField(value, "operatorDetail", "operator_detail") ?? remembered?.operatorDetail;
  const impact = stringField(value, "impact") ?? remembered?.impact;
  const reasonCode = stringField(value, "reasonCode", "reason_code") ?? remembered?.reasonCode;
  const href = reference
    ? `/settings?diagnostic=${encodeURIComponent(reference)}#diagnostics-settings`
    : "/settings#diagnostics-settings";
  const classes = ["diagnostic-error-notice", compact ? "compact" : undefined, className]
    .filter(Boolean)
    .join(" ");
  const Root = compact ? "span" : "div";

  return (
    <Root className={classes} role="alert" data-error-reference={reference}>
      <ShieldAlert size={16} />
      <span>
        <strong>{title ?? boundedReason(rawMessage, fallback)}</strong>
        {title && <small>{boundedReason(rawMessage, fallback)}</small>}
        {operatorDetail && <small><b>Cause:</b> {boundedReason(operatorDetail, fallback)}</small>}
        {impact && <small><b>Impact:</b> {boundedReason(impact, "Impact was not classified.")}</small>}
        <small>
          {retryable === true
            ? "This operation can be retried."
            : retryable === false
              ? "No verified retry procedure is available."
              : "Review Diagnostics for the recorded cause and recovery guidance."}
        </small>
        <small>Reference: {reference ?? "pending local diagnostic"}{reasonCode ? ` · ${humanize(reasonCode)}` : code ? ` · ${code}` : ""}</small>
      </span>
      <a href={href}>View diagnostics</a>
    </Root>
  );
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replaceAll("-", " ");
}
