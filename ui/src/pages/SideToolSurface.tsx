import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Maximize2, Minimize2, X } from "lucide-react";
import { useDialogPresence } from "../components/DialogSystem";
import { sideToolLabel, type SideToolTab } from "./sidePanelTabs";

const focusableSelector = 'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [contenteditable="true"], [tabindex]:not([tabindex="-1"])';

export function SideToolSurface({ activeTab, visible, onClose, children }: {
  activeTab?: SideToolTab;
  visible: boolean;
  onClose: (id: string) => void;
  children: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const surface = useRef<HTMLElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const fullscreen = expanded && visible && Boolean(activeTab);
  const label = activeTab ? sideToolLabel(activeTab) : "Side panels";
  useDialogPresence(fullscreen);

  useEffect(() => {
    if (!visible || !activeTab) setExpanded(false);
  }, [visible, activeTab?.id]);

  useLayoutEffect(() => {
    if (!fullscreen || !surface.current) return;
    const inertSiblings: Array<[HTMLElement, boolean]> = [];
    for (let branch: HTMLElement | null = surface.current; branch?.parentElement; branch = branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling !== branch && sibling instanceof HTMLElement) {
          inertSiblings.push([sibling, sibling.inert]);
          sibling.inert = true;
        }
      }
    }
    const bodyOverflow = document.body.style.overflow;
    const documentOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    toggle.current?.focus({ preventScroll: true });
    return () => {
      for (const [sibling, inert] of inertSiblings) sibling.inert = inert;
      document.body.style.overflow = bodyOverflow;
      document.documentElement.style.overflow = documentOverflow;
    };
  }, [fullscreen]);

  useEffect(() => {
    if (!fullscreen) return;
    const restoreOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape" || document.querySelector(".dialog-backdrop .modal-surface")) return;
      event.preventDefault();
      event.stopPropagation();
      setExpanded(false);
      toggle.current?.focus({ preventScroll: true });
    };
    document.addEventListener("keydown", restoreOnEscape, true);
    return () => document.removeEventListener("keydown", restoreOnEscape, true);
  }, [fullscreen]);

  const trapFocus = (event: KeyboardEvent<HTMLElement>) => {
    if (!fullscreen || event.key !== "Tab") return;
    const items = [...(surface.current?.querySelectorAll<HTMLElement>(focusableSelector) ?? [])]
      .filter(item => item.getClientRects().length && !item.closest("[inert]"));
    const first = items[0], last = items.at(-1);
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return <section
    ref={surface}
    id="side-tool-panel"
    className={`side-tool-panel${fullscreen ? " is-fullscreen" : ""}`}
    role={fullscreen ? "dialog" : visible && activeTab ? "region" : undefined}
    aria-modal={fullscreen || undefined}
    aria-label={fullscreen ? `${label} full screen` : visible && activeTab ? `${label} beside chat` : undefined}
    data-active={Boolean(activeTab && visible)}
    hidden={!visible}
    onKeyDown={trapFocus}
  >
    <header className="side-chat-header">
      <div><strong>{label}</strong><small>{activeTab?.kind === "code" ? "Workspace files" : "Beside this conversation"}</small></div>
      {activeTab && <>
        <button ref={toggle} className="icon-button subtle" type="button"
          aria-label={`${fullscreen ? "Restore" : "Expand"} ${label} ${fullscreen ? "to split view" : "to full screen"}`}
          title={fullscreen ? `Restore ${label} to split view (Escape)` : `Expand ${label} to full screen`}
          aria-expanded={fullscreen}
          onClick={() => setExpanded(value => !value)}>
          {fullscreen ? <Minimize2 size={18} aria-hidden="true" /> : <Maximize2 size={18} aria-hidden="true" />}
        </button>
        <button className="icon-button subtle" type="button" aria-label={`Close ${label} tab`} title={`Close ${label} tab`} onClick={() => { setExpanded(false); onClose(activeTab.id); }}><X size={16} aria-hidden="true" /></button>
      </>}
    </header>
    {children}
  </section>;
}
