import { reasoningSummaryState, reasoningSummaryText, type HarnessActivityItem } from "../pages/harnessActivity";

/** Render only the harness's public display fields, never its raw trace payload. */
export function HarnessReasoningDetails({ item }: { item: HarnessActivityItem }) {
  const text = reasoningSummaryText(item);
  const state = reasoningSummaryState(item);
  const earlier = item.payload.reasoning_streamed_text;
  return <>
    {(item.streams.commentary || item.summary) && <p className={item.kind === "reasoning" ? "harness-reasoning-summary" : undefined} tabIndex={item.kind === "reasoning" ? 0 : undefined}>{item.streams.commentary || item.summary}</p>}
    {text && <p className="harness-reasoning-summary" tabIndex={0}>{text}</p>}
    {state === "pending" && !text && <p>Thinking is in progress. Text will appear if the harness provides it.</p>}
    {state === "not_provided" && <p>No thinking summary was provided by the harness.</p>}
    {typeof earlier === "string" && earlier !== text && <details className="harness-streamed-summary"><summary>Earlier streamed summary</summary><p className="harness-reasoning-summary" tabIndex={0}>{earlier}</p></details>}
    {(item.payload.reasoning_summary_truncated === true || text?.includes("…[truncated]")) && <p>This saved thinking text was shortened. The omitted text is unavailable.</p>}
    {item.payload.reasoning_summary_malformed === true && <p>Some summary content could not be read. Any readable text is shown here.</p>}
    {state && <small className="harness-reasoning-note">{item.vendor === "codex_app_server" ? "Provider-supplied reasoning summary." : "Thinking text provided by the harness."}</small>}
  </>;
}
