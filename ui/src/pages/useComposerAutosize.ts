import {useLayoutEffect, type RefObject} from "react";

/** Draft text and available width jointly determine the textarea's height. */
export function useComposerAutosize(
  ref: RefObject<HTMLTextAreaElement | null>,
  draft: string,
  maxHeight: number,
  identity: string,
) {
  useLayoutEffect(() => {
    const textarea = ref.current;
    if (!textarea) return;
    const fit = () => {
      if (!draft) {
        textarea.style.height = "";
        textarea.style.overflowY = "hidden";
        return;
      }
      // Measure content without the previous/minimum box height. WebKit can
      // otherwise include the minimum height plus padding in scrollHeight.
      const minimum = textarea.style.minHeight;
      textarea.style.minHeight = "0px";
      textarea.style.height = "0px";
      const contentHeight = textarea.scrollHeight;
      textarea.style.minHeight = minimum;
      textarea.style.height = `${Math.min(contentHeight, maxHeight)}px`;
      textarea.style.overflowY = contentHeight > maxHeight ? "auto" : "hidden";
    };
    let previousWidth = textarea.getBoundingClientRect().width;
    const resized = () => {
      const width = textarea.getBoundingClientRect().width;
      // Setting height itself triggers the observer. Only width invalidates
      // line wrapping, so ignore height notifications and avoid a resize loop.
      if (Math.abs(width - previousWidth) < 0.5) return;
      previousWidth = width;
      fit();
    };
    fit();
    const observer = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(resized);
    observer?.observe(textarea);
    window.addEventListener("resize", resized);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", resized);
    };
  }, [ref, draft, maxHeight, identity]);
}
