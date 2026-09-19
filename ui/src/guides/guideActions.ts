import { useEffect, useRef } from "react";

/** Transient panels a guide may open. Pages own the state; guides only ask. */
export type GuideAction = "open-assistant-settings" | "open-palette";

const GUIDE_ACTION_EVENT = "nebula:guide-action";

export function requestGuideAction(action: GuideAction): void {
  window.dispatchEvent(new CustomEvent<GuideAction>(GUIDE_ACTION_EVENT, { detail: action }));
}

export function useGuideAction(action: GuideAction, handler: () => void): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  useEffect(() => {
    const listener = (event: Event) => {
      if ((event as CustomEvent<GuideAction>).detail === action) handlerRef.current();
    };
    window.addEventListener(GUIDE_ACTION_EVENT, listener);
    return () => window.removeEventListener(GUIDE_ACTION_EVENT, listener);
  }, [action]);
}

/** Popovers that close on outside pointers must stay open while the guide card is used. */
export function isGuideLayerTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest("[data-guide-layer]"));
}
