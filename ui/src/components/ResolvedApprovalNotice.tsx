import { CheckCircle2, RefreshCw } from "lucide-react";

export function ResolvedApprovalNotice({ status, busy, canStop, onCheck, onStop }: {
  status: string; busy: boolean; canStop: boolean; onCheck: () => void; onStop: () => void;
}) {
  return <section className="chat-resolved-approval" aria-label="Recorded approval decision" role="status">
    <CheckCircle2 size={18} aria-hidden="true" />
    <div><strong>Decision recorded: {status}</strong>
      <p>{canStop ? "This response is still paused. Check its status or stop it before starting another response." : "This response is still paused. Check its status; this harness does not support stopping from here."}</p>
    </div>
    <div className="chat-resolved-actions">
      <button className="icon-button subtle" type="button" aria-label="Check response status" title="Check status" disabled={busy} onClick={onCheck}><RefreshCw size={17} aria-hidden="true" /></button>
      {canStop && <button className="button secondary" type="button" disabled={busy} onClick={onStop}>Stop waiting</button>}
    </div>
  </section>;
}
