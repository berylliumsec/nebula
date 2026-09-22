import { IconAction } from "./IconAction";
import { X } from "lucide-react";
import { createPortal } from "react-dom";
import { useEffect, useId, useState, type ReactNode } from "react";
import { ModalSurface } from "./DialogSystem";
import { useResizableSidePanel } from "./useResizableSidePanel";

export function ChatWorkspaceDrawer({children, tab, onTab, onClose, onWidthChange, minPrimaryWidth = 420, overlay = false}: {overlay?: boolean; children: ReactNode; tab: string; onTab: (tab: "context" | "results" | "subagents") => void; onClose: () => void; onWidthChange?: (width: number | undefined) => void; minPrimaryWidth?: number}) {
  const [mobile, setMobile] = useState(() => matchMedia("(max-width: 1100px)").matches);
  const id = useId();
  useEffect(() => {const media = matchMedia("(max-width: 1100px)"); const update = () => setMobile(media.matches); media.addEventListener("change", update); return () => media.removeEventListener("change", update);}, []);
  const size = useResizableSidePanel({
    defaultWidth: 280,
    enabled: !mobile && !overlay,
    label: "Resize conversation details",
    maxWidth: 640,
    minPrimaryWidth,
    minWidth: 280,
    onWidthChange,
    storageKey: "nebula.session-inspector.width",
  });
  const content = <><header><strong id={id}>Conversation details</strong><IconAction icon={X} label="Close details" onClick={onClose} /></header><nav aria-label="Conversation detail views">{(["context", "results", "subagents"] as const).map(item => <button className="button quiet" aria-current={tab === item ? "page" : undefined} key={item} onClick={() => onTab(item)}>{item === "context" ? "Context" : item === "results" ? "Results" : "Subagents"}</button>)}</nav>{children}</>;
  return mobile || overlay ? createPortal(<ModalSurface as="section" className="session-inspector assistant-drawer" labelledBy={id} onClose={onClose}>{content}</ModalSurface>, document.body) : <aside ref={(element) => { size.panelRef.current = element; }} className="session-inspector assistant-drawer" aria-label="Session inspector" style={size.panelStyle}>{size.resizeHandle}{content}</aside>;
}
