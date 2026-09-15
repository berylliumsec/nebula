import { LoaderCircle, Pause, Play } from "lucide-react";

interface Props {
  enabled: boolean;
  available: boolean;
  desktop: boolean;
  scopeReady: boolean;
  nativeReady: boolean;
  pending: boolean;
  onToggle: () => void;
  onSetup: () => void;
}

export function InterceptionTransport({ enabled, available, desktop, scopeReady, nativeReady, pending, onToggle, onSetup }: Props) {
  return <div className="browser-interception-transport" aria-label="Interception controls">
    <span>{!scopeReady ? "No Project target · add an allowed target before requests can pause" : !nativeReady ? "Open a native tab to start the capture proxy" : enabled ? "Interception on · new in-scope requests pause" : "Interception off · new requests pass through"}</span>
    {available ? <button className="button secondary" type="button" disabled={pending || (!enabled && (!scopeReady || !nativeReady))} aria-pressed={enabled} onClick={onToggle} title={!scopeReady && !enabled ? "Add an allowed target to Project scope before pausing" : !nativeReady && !enabled ? "Open a native tab to start the capture proxy" : enabled ? "Let new requests pass through. Already paused requests still need a decision." : "Pause new in-scope requests for inspection"}>
      {pending ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : enabled ? <Play size={16} aria-hidden="true" /> : <Pause size={16} aria-hidden="true" />}
      {enabled ? "Resume requests" : "Pause requests"}
    </button> : <button className="button secondary" type="button" onClick={onSetup}>{desktop ? "Set up interception" : "View desktop connection"}</button>}
  </div>;
}
