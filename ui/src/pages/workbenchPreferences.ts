const CONVERSATION_PANEL_KEY = "nebula.conversations.open";
const LEGACY_CONVERSATION_PANEL_KEY = "nebula.conversations.expanded";

type PreferenceStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function readConversationPanelOpen(storage: PreferenceStorage): boolean {
  const saved = storage.getItem(CONVERSATION_PANEL_KEY);
  if (saved !== null) return saved === "true";

  const legacyOpen = storage.getItem(LEGACY_CONVERSATION_PANEL_KEY) === "true";
  if (legacyOpen) storage.setItem(CONVERSATION_PANEL_KEY, "true");
  storage.removeItem(LEGACY_CONVERSATION_PANEL_KEY);
  return legacyOpen;
}

export function writeConversationPanelOpen(storage: PreferenceStorage, open: boolean): void {
  storage.setItem(CONVERSATION_PANEL_KEY, String(open));
}
