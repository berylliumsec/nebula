import { createContext, useContext } from "react";

export interface NebulaAssistantLauncherState {
  opened: boolean;
  visible: boolean;
  toggle: () => void;
}
export const NebulaAssistantLauncherContext = createContext<NebulaAssistantLauncherState | null>(null);

export function NebulaAssistantLauncherButton({ mobile = false }: { mobile?: boolean }) {
  const assistant = useContext(NebulaAssistantLauncherContext);
  if (!assistant) return null;
  const label = assistant.visible ? "Toggle Butler" : assistant.opened ? "Show Butler" : "Open Butler";
  return <button className={mobile ? undefined : "icon-button toolbar-button nebula-assistant-trigger"} type="button" aria-label={label}
    aria-expanded={assistant.visible} aria-controls="nebula-assistant-panel" title="Butler" onClick={assistant.toggle}>
    <img className="nebula-assistant-mark" src="/nebula-logo.svg" width="18" height="29" alt="" aria-hidden="true" />{mobile && <span>Butler</span>}
  </button>;
}
