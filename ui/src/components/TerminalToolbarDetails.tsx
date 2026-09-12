import { useId, useState, type ReactNode } from "react";
import { X } from "lucide-react";
import { ModalSurface } from "./DialogSystem";

export function TerminalToolbarDetails({label, icon, children}: {label: string; icon: ReactNode; children: ReactNode}) {
  const [open, setOpen] = useState(false);
  const titleId = useId();
  return <>
    <button className="icon-button subtle" type="button" aria-label={label} title={label} aria-haspopup="dialog" onClick={() => setOpen(true)}>{icon}</button>
    {open && <ModalSurface className="terminal-toolbar-details" labelledBy={titleId} onClose={() => setOpen(false)}>
      <header><strong id={titleId}>{label}</strong><button className="icon-button subtle" type="button" aria-label={`Close ${label.toLowerCase()}`} title="Close" onClick={() => setOpen(false)}><X size={16} aria-hidden="true" /></button></header>
      <div className="terminal-toolbar-details-content">{children}</div>
    </ModalSurface>}
  </>;
}
