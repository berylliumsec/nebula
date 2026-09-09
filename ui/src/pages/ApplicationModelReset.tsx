import { useEffect, useRef, useState } from "react";
import { RotateCcw, X } from "lucide-react";
import { ModalSurface } from "../components/DialogSystem";
import { cryptoId } from "./ApplicationModelEditors";

type Preview = { revision: number; objects: number; relationships: number; captures: number; custom_definitions: number };
type ResetRequest = { expected_revision: number; idempotency_key: string; confirmation: "clear-model-and-browser-captures" };
const countLabel = (count: number, noun: string) => `${count} ${noun}${count === 1 ? "" : "s"}`;
export function ApplicationModelReset({ base, projectName, request, onReset, disabled }: {
  base: string; projectName: string; disabled: boolean;
  request: (path: string, options?: { method: string; body: string }) => Promise<unknown>;
  onReset: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<Preview>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const pending = useRef<ResetRequest | undefined>(undefined);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const inFlight = useRef(false);
  const generation = useRef(0);
  const close = () => { if (!inFlight.current) { generation.current++; setOpen(false); } };
  const review = async () => {
    const current = ++generation.current;
    setOpen(true); setPreview(undefined); setError(""); setConflict(false); pending.current = undefined;
    try {
      const result = await request(`${base}/reset-preview`) as Preview;
      if (!alive.current || current !== generation.current) return;
      setPreview(result);
      pending.current = { expected_revision: result.revision, idempotency_key: cryptoId(), confirmation: "clear-model-and-browser-captures" };
    } catch { if (alive.current && current === generation.current) setError("Could not load reset details. Nothing has been cleared."); }
  };
  const clear = async () => {
    if (!pending.current || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError("");
    try {
      await request(`${base}/reset`, { method: "POST", body: JSON.stringify(pending.current) });
      if (alive.current) { setOpen(false); onReset(); }
    } catch (e) {
      if (!alive.current) return;
      const changed = (e as { status?: number })?.status === 409 || /409|model changed/i.test(String(e));
      setConflict(changed);
      setError(changed ? "The model changed. Review the latest counts before clearing." : "Could not confirm the reset. Retry safely with the same request.");
    } finally { inFlight.current = false; if (alive.current) setBusy(false); }
  };
  return <>
    <button className="icon-button subtle am-reset-trigger" aria-label="Start over" title="Start over" disabled={disabled} onClick={() => void review()}>
      <RotateCcw size={17} aria-hidden="true" />
    </button>
    {open && <ModalSurface labelledBy="model-reset-title" className="confirmation-dialog am-reset-dialog" onClose={close}>
      <header>
        <div><h2 id="model-reset-title">Start this model over?</h2><p className="am-hint am-reset-project">{projectName}</p></div>
        <button className="icon-button subtle" aria-label="Close reset" title="Close" disabled={busy} onClick={close}><X size={17} aria-hidden="true" /></button>
      </header>
      {preview ? <p className="am-reset-counts">{countLabel(preview.objects, "object")} · {countLabel(preview.relationships, "relationship")} · {countLabel(preview.captures, "capture")}</p> : <p role="status">{error ? "Reset details unavailable." : "Loading reset details…"}</p>}
      <p>Clears this model, its custom definitions and history, and recorded browser captures. This cannot be undone.</p>
      <p className="am-hint">Other evidence, findings, chats, files, tabs and sign-ins stay. Shared attachments are kept. Reload open pages afterward to capture ongoing connections again.</p>
      <p className="am-hint">Unsaved model edits will also be discarded.</p>
      {error && <p role="alert">{error}</p>}
      <footer>
        <button className="button secondary" data-autofocus disabled={busy} onClick={close}>Cancel</button>
        {(!preview && error) || conflict ? <button className="button secondary" onClick={() => void review()}>Review latest counts</button> :
          <button className="button danger" disabled={!preview || busy} onClick={() => void clear()}>{busy ? "Clearing…" : error ? "Retry clear" : "Clear and start over"}</button>}
      </footer>
    </ModalSurface>}
  </>;
}
