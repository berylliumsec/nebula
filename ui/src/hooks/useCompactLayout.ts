import { useEffect, useState } from "react";

/** Phone-width layout, shared by the CSS breakpoint of the same width. */
export const COMPACT_LAYOUT_QUERY = "(max-width: 760px)";

function matches(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(COMPACT_LAYOUT_QUERY).matches;
}

export function useCompactLayout(): boolean {
  const [compact, setCompact] = useState(matches);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(COMPACT_LAYOUT_QUERY);
    const update = () => setCompact(query.matches);
    update();
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, []);
  return compact;
}

/** The private iPhone app appends this token to WKWebView's user agent. */
export function isNebulaShell(userAgent = typeof navigator === "undefined" ? "" : navigator.userAgent): boolean {
  return /\bNebulaShell\/\d/.test(userAgent);
}
