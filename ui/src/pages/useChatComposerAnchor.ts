import { useLayoutEffect, type RefObject } from "react";

/** Keep settings above the actual composer, including wrapped mobile controls. */
export function useChatComposerAnchor(input: RefObject<HTMLTextAreaElement | null>, active: boolean, surface?: string) {
  useLayoutEffect(() => {
    if (!active) return;
    const composer = input.current?.closest<HTMLElement>(".chat-composer");
    const panel = composer?.closest<HTMLElement>(".chat-panel");
    if (!composer || !panel) return;
    const update = () => panel.style.setProperty("--composer-anchor", `${Math.max(6, panel.getBoundingClientRect().bottom - composer.getBoundingClientRect().top + 6)}px`);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(composer); observer.observe(panel);
    return () => observer.disconnect();
  }, [input, active, surface]);
}
