import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const controls = "button, a, summary, [role=button], [role=tab]";

/** One tooltip layer also covers lazy screens, dialogs and disabled icon buttons. */
export function ActionTooltips() {
  const id = useId();
  const [active, setActive] = useState<{ anchor: HTMLElement; text: string } | null>(null);
  const tip = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let anchor: HTMLElement | null = null;
    let originalTitle: string | null = null;
    let originalDescription: string | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let touch = false;
    const hide = () => {
      clearTimeout(timer);
      if (anchor) {
        if (originalTitle !== null) anchor.setAttribute("title", originalTitle);
        if (originalDescription === null) anchor.removeAttribute("aria-describedby");
        else anchor.setAttribute("aria-describedby", originalDescription);
      }
      anchor = null;
      setActive(null);
    };
    const show = (target: EventTarget | null, immediate = false) => {
      const next = target instanceof Element ? target.closest<HTMLElement>(controls) : null;
      if (next === anchor) return;
      hide();
      if (!next) return;
      const text = next.getAttribute("title") || next.getAttribute("aria-label") ||
        (next.getAttribute("aria-labelledby") || "").split(/\s+/).map(label => document.getElementById(label)?.textContent || "").join(" ").trim();
      if (!text) return;
      anchor = next;
      originalTitle = next.getAttribute("title");
      originalDescription = next.getAttribute("aria-describedby");
      // Suppress the delayed native popup while the app tooltip is responsible.
      next.removeAttribute("title");
      const reveal = () => {
        if (!next.isConnected || anchor !== next) return;
        next.setAttribute("aria-describedby", [originalDescription, id].filter(Boolean).join(" "));
        setActive({ anchor: next, text });
      };
      if (immediate) reveal();
      else timer = setTimeout(reveal, 200);
    };
    const over = (event: PointerEvent) => { touch = event.pointerType === "touch"; if (!touch) show(event.target); };
    const out = (event: PointerEvent) => {
      if (anchor && event.relatedTarget instanceof Node && anchor.contains(event.relatedTarget)) return;
      hide();
    };
    const focus = (event: FocusEvent) => { if (!touch) show(event.target, true); };
    const key = (event: KeyboardEvent) => { touch = false; if (event.key === "Escape") hide(); };
    document.addEventListener("pointerover", over, true);
    document.addEventListener("pointerout", out, true);
    document.addEventListener("pointerdown", hide, true);
    document.addEventListener("focusin", focus, true);
    document.addEventListener("focusout", hide, true);
    document.addEventListener("keydown", key, true);
    document.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      hide();
      document.removeEventListener("pointerover", over, true);
      document.removeEventListener("pointerout", out, true);
      document.removeEventListener("pointerdown", hide, true);
      document.removeEventListener("focusin", focus, true);
      document.removeEventListener("focusout", hide, true);
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
    };
  }, [id]);
  useLayoutEffect(() => {
    if (!active || !tip.current) return;
    const box = active.anchor.getBoundingClientRect();
    const label = tip.current.getBoundingClientRect();
    tip.current.style.left = `${Math.max(8, Math.min(innerWidth - label.width - 8, box.left + (box.width - label.width) / 2))}px`;
    const top = box.top >= label.height + 16 ? box.top - label.height - 8 : box.bottom + 8;
    tip.current.style.top = `${Math.max(8, Math.min(innerHeight - label.height - 8, top))}px`;
  }, [active]);
  return active ? createPortal(<div ref={tip} id={id} role="tooltip" className="action-tooltip">{active.text}</div>, active.anchor.closest("dialog[open]") ?? document.body) : null;
}
