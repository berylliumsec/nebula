/** ⌘↵ on Apple devices, Ctrl+↵ elsewhere: send the draft now, stopping the running response. */
export function isSendNowShortcut(event: { key: string; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean; altKey: boolean; nativeEvent?: { isComposing?: boolean } }): boolean {
  return event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.shiftKey && !event.altKey && !event.nativeEvent?.isComposing;
}

function applePlatform(): boolean {
  const nav = globalThis.navigator as (Navigator & { userAgentData?: { platform?: string } }) | undefined;
  const platform = nav?.userAgentData?.platform ?? nav?.platform ?? "";
  return /mac|iphone|ipad|ipod/i.test(platform);
}

/** The key hint shown beside Send now. */
export function sendNowShortcutLabel(): string {
  return applePlatform() ? "⌘↵" : "Ctrl+↵";
}
