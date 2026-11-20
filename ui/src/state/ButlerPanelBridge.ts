import { createContext, useContext } from "react";

/**
 * Where Butler mode shows the Butler panel's content: a project's ongoing
 * conversation ("chat") in the thread column, or Butler's own settings
 * ("more") in the Settings tab. One panel instance owns that state; Butler
 * mode lends it a place on the page.
 */
export interface ButlerPanelEmbed {
  host: HTMLElement;
  view: "chat" | "more";
}

export interface ButlerPanelBridge {
  /** Show the panel inside ``embed.host``; undefined returns it to its floating place. */
  embed: (embed: ButlerPanelEmbed | undefined) => void;
  /** Open the small floating panel (Compact). */
  openCompact: () => void;
  /** Start Butler Voice in the selected project. */
  startVoice: () => void;
  voiceAvailable: boolean;
  /** Open incidents in the selected project. */
  attentionCount: number;
}

export const ButlerPanelBridgeContext = createContext<ButlerPanelBridge | null>(null);

export function useButlerPanelBridge(): ButlerPanelBridge | null {
  return useContext(ButlerPanelBridgeContext);
}

/** Butler mode for a project, optionally on one of its sections. */
export function butlerModePath(projectId: string | undefined, section?: "board" | "code" | "oversight" | "memory" | "settings"): string {
  const params = new URLSearchParams();
  if (projectId) params.set("project", projectId);
  if (section && section !== "board") params.set("section", section);
  const query = params.toString();
  return `/butler${query ? `?${query}` : ""}`;
}
