import { type ReactNode, useLayoutEffect, useRef } from "react";
import { Maximize2, Minimize2 } from "lucide-react";
import { useDialogPresence } from "./DialogSystem";

/** Keep the same content mounted so expanding never discards editor drafts. */
export function ExpandableModelPanel({
  title, label = title, as: Surface = "section", className, expanded,
  onExpand, onRestore, children,
}: {
  title: string;
  label?: string;
  as?: "section" | "aside";
  className: string;
  expanded: boolean;
  onExpand: () => void;
  onRestore: () => void;
  children: ReactNode;
}) {
  const surface = useRef<HTMLElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  useDialogPresence(expanded);
  useLayoutEffect(() => {
    if (!expanded || !surface.current) return;
    const element = surface.current;
    const scrollTop = element.scrollTop;
    const overflow = document.body.style.overflow;
    const siblings: [HTMLElement, boolean][] = [];
    // Inert every branch outside this panel, including shell navigation.
    for (let branch: HTMLElement | null = element; branch?.parentElement; branch = branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling !== branch && sibling instanceof HTMLElement) {
          siblings.push([sibling, sibling.inert]);
          sibling.inert = true;
        }
      }
    }
    document.body.style.overflow = "hidden";
    toggle.current?.focus({ preventScroll: true });
    return () => {
      siblings.forEach(([sibling, inert]) => { sibling.inert = inert; });
      document.body.style.overflow = overflow;
      element.scrollTop = scrollTop;
      toggle.current?.focus({ preventScroll: true });
      // React restores the formerly focused node during its DOM commit.
      // Restore again afterwards, unless another panel has taken over.
      queueMicrotask(() => {
        if (element.isConnected && !document.querySelector(".am-fullscreen")) {
          toggle.current?.focus({ preventScroll: true });
        }
      });
    };
  }, [expanded]);
  return (
    <Surface
      ref={surface as React.Ref<HTMLElement>}
      className={`${className} am-expandable ${expanded ? "am-fullscreen" : ""}`}
      role={expanded ? "dialog" : undefined}
      aria-modal={expanded || undefined}
      aria-label={label}
      onKeyDown={(event) => {
        if (!expanded) return;
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          onRestore();
        }
        if (event.key !== "Tab") return;
        const items = [...(surface.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]',
        ) ?? [])].filter((item) => item.getClientRects().length && !item.closest("[inert]"));
        const first = items[0], last = items.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault(); first?.focus();
        }
      }}
    >
      <header className="am-panel-heading">
        <h2>{title}</h2>
        <button
          ref={toggle}
          type="button"
          className="icon-button subtle"
          aria-label={`${expanded ? "Restore" : "Expand"} ${title.toLowerCase()}`}
          title={expanded ? "Restore panel (Escape)" : "Expand to full screen"}
          aria-expanded={expanded}
          onClick={expanded ? onRestore : onExpand}
        >
          {expanded ? <Minimize2 size={18} aria-hidden="true" /> : <Maximize2 size={18} aria-hidden="true" />}
        </button>
      </header>
      {children}
    </Surface>
  );
}
