import { createPortal } from "react-dom";
import { useEffect, useId, useState, type ReactNode } from "react";
import { ModalSurface } from "./DialogSystem";

export function ChatWorkspaceDrawer({children, tab, onTab, onClose, overlay = false}: {overlay?: boolean; children: ReactNode; tab: string; onTab: (tab: "context" | "results") => void; onClose: () => void}) {
  const [mobile, setMobile] = useState(() => matchMedia("(max-width: 1100px)").matches);
  const id = useId();
  useEffect(() => {const media = matchMedia("(max-width: 1100px)"); const update = () => setMobile(media.matches); media.addEventListener("change", update); return () => media.removeEventListener("change", update);}, []);
  const content = <><header><strong id={id}>Conversation details</strong><button className="button quiet" onClick={onClose}>Close details</button></header><nav aria-label="Conversation detail views">{(["context", "results"] as const).map(item => <button className="button quiet" aria-current={tab === item ? "page" : undefined} key={item} onClick={() => onTab(item)}>{item === "context" ? "Context" : "Results"}</button>)}</nav>{children}</>;
  return mobile || overlay ? createPortal(<ModalSurface as="section" className="session-inspector assistant-drawer" labelledBy={id} onClose={onClose}>{content}</ModalSurface>, document.body) : <aside className="session-inspector assistant-drawer" aria-label="Session inspector">{content}</aside>;
}
