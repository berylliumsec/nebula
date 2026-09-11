import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export const BROWSER_ASSISTANT_SHEET_QUERY = "(max-width: 760px), (pointer: coarse) and (max-height: 500px)";

/** Desktop column or a nonmodal phone sheet, retaining the same conversation. */
export function BrowserAssistantPanel({ header, children, onActionContainer, panelId = "browser-assistant-panel", label = "Browser Assistant" }: {
  header: ReactNode; children: ReactNode;
  onActionContainer: (element: HTMLDivElement | null) => void;
  panelId?: string;
  label?: string;
}) {
  const measure = () => {
    const viewport = window.visualViewport;
    const height = viewport?.height ?? window.innerHeight;
    const keyboard = height < window.innerHeight - 120;
    const reserve = keyboard ? 8 : 76;
    return { mobile: window.matchMedia(BROWSER_ASSISTANT_SHEET_QUERY).matches,
      bottom: window.innerHeight - (viewport?.offsetTop ?? 0) - height + reserve,
      height: Math.min(560, height - reserve - 12, Math.max(240, height * .68)) };
  };
  const [viewport, setViewport] = useState(measure);
  useEffect(() => {
    const update = () => setViewport(measure());
    window.addEventListener("resize", update);
    window.visualViewport?.addEventListener("resize", update);
    window.visualViewport?.addEventListener("scroll", update);
    return () => {
      window.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("resize", update);
      window.visualViewport?.removeEventListener("scroll", update);
    };
  }, []);
  const panel = <aside id={panelId} className={`integrated-browser-assistant${viewport.mobile ? " browser-assistant-sheet" : ""}`} aria-label={label}
    style={viewport.mobile ? { bottom: `calc(${viewport.bottom}px + env(safe-area-inset-bottom, 0px))`, height: `calc(${viewport.height}px - env(safe-area-inset-bottom, 0px))` } : undefined}>
    <header>{header}</header>
    {viewport.mobile && <div ref={onActionContainer} className="browser-assistant-required-actions" />}
    {children}
  </aside>;
  return viewport.mobile ? createPortal(panel, document.body) : panel;
}
