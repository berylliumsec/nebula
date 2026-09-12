import { LoaderCircle, Pause, Play } from "lucide-react";

interface Props {
  enabled: boolean;
  available: boolean;
  desktop: boolean;
  pending: boolean;
  onToggle: () => void;
  onSetup: () => void;
}

export function InterceptionTransport({ enabled, available, desktop, pending, onToggle, onSetup }: Props) {
  return <div className="browser-interception-transport" aria-label="Interception controls">
    <span>{enabled ? "Interception on · new requests pause" : "Interception off · new requests pass through"}</span>
    {available ? <button className="button secondary" type="button" disabled={pending} aria-pressed={enabled} onClick={onToggle} title={enabled ? "Let new requests pass through. Already paused requests still need a decision." : "Pause new in-scope requests for inspection"}>
      {pending ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : enabled ? <Play size={16} aria-hidden="true" /> : <Pause size={16} aria-hidden="true" />}
      {enabled ? "Resume requests" : "Pause requests"}
    </button> : <button className="button secondary" type="button" onClick={onSetup}>{desktop ? "Set up interception" : "View desktop connection"}</button>}
  </div>;
}
