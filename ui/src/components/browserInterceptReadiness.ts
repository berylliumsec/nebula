const READY_ATTRIBUTE = "data-browser-intercept-listener-ready";
const READY_EVENT = "nebula-browser-intercept-listener-ready";

export function markBrowserInterceptListenerReady(): void {
  document.documentElement.setAttribute(READY_ATTRIBUTE, "true");
  window.dispatchEvent(new Event(READY_EVENT));
}

export function clearBrowserInterceptListenerReady(): void {
  document.documentElement.removeAttribute(READY_ATTRIBUTE);
}

export function browserInterceptListenerReady(): boolean {
  return document.documentElement.getAttribute(READY_ATTRIBUTE) === "true";
}

export async function waitForBrowserInterceptListener(timeoutMs = 5_000): Promise<void> {
  if (browserInterceptListenerReady()) return;
  await new Promise<void>((resolve, reject) => {
    const ready = () => finish();
    const finish = () => {
      window.clearTimeout(timer);
      window.removeEventListener(READY_EVENT, ready);
      resolve();
    };
    const timer = window.setTimeout(() => {
      window.removeEventListener(READY_EVENT, ready);
      reject(new Error("Browser interception is still starting. Retry after the desktop connection is ready."));
    }, timeoutMs);
    window.addEventListener(READY_EVENT, ready, { once: true });
    if (browserInterceptListenerReady()) finish();
  });
}
