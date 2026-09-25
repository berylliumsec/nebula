import { Check, ChevronRight, Copy } from "lucide-react";
import { useEffect, useState } from "react";
import type { ChatWaitKind } from "../api/types";

type CallbackWaitingStatusProps = {
  summary: string;
  resultsUrl?: string;
  /** Subagent waits reuse this status line; only a command wait has a callback. */
  kind?: ChatWaitKind;
};

const WAIT_LABELS: Record<ChatWaitKind, string> = {
  process: "Waiting for command results",
  subagents: "Waiting for subagents",
  reply: "Waiting for the delegating assistant",
};

export function CallbackWaitingStatus({ summary, resultsUrl, kind }: CallbackWaitingStatusProps) {
  const callback = kind === undefined || kind === "process";
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => setCopyState("idle"), [resultsUrl]);

  const copyResultsUrl = async () => {
    if (!resultsUrl) return;
    try {
      await navigator.clipboard.writeText(resultsUrl);
      setCopyState("copied");
    } catch { // diagnostic-expected: clipboard denial is shown beside the selectable URL.
      setCopyState("failed");
    }
  };

  return <section className="callback-waiting-status" role="status" aria-label={WAIT_LABELS[kind ?? "process"]}>
    <header>
      <span className="callback-waiting-dot" aria-hidden="true" />
      <strong>{summary}</strong>
      {callback && <small>{copyState === "copied" ? "Copied" : "Callback ready"}</small>}
    </header>
    {resultsUrl && <>
      <div className="callback-results-endpoint">
        <code title={resultsUrl}>{resultsUrl}</code>
        <button className="icon-button subtle quiet-icon-action" type="button" aria-label="Copy results URL" title="Copy results URL" onClick={() => void copyResultsUrl()}>
          {copyState === "copied" ? <Check size={18} aria-hidden="true" /> : <Copy size={18} aria-hidden="true" />}
        </button>
      </div>
      {copyState === "failed" && <p className="callback-copy-error" role="alert">Clipboard access was unavailable. Select and copy the URL instead.</p>}
      <details className="callback-waiting-details">
        <summary><ChevronRight size={14} aria-hidden="true" /> How this callback works</summary>
        <p>The command received an API key in <code>NEBULA_RESULTS_KEY</code>. It should POST the result to this LAN URL.</p>
      </details>
    </>}
  </section>;
}
