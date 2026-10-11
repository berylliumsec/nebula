import { createContext, useContext } from "react";
import { Bot } from "lucide-react";

export interface NebulaAssistantLauncherState {
  opened: boolean;
  visible: boolean;
  toggle: () => void;
}
export const NebulaAssistantLauncherContext = createContext<NebulaAssistantLauncherState | null>(null);

export function NebulaAssistantLauncherButton({ mobile = false }: { mobile?: boolean }) {
  const assistant = useContext(NebulaAssistantLauncherContext);
  if (!assistant) return null;
  const label = assistant.visible ? "Toggle Nebula assistant" : assistant.opened ? "Show Nebula assistant" : "Open Nebula assistant";
  return <button className={mobile ? undefined : "icon-button toolbar-button nebula-assistant-trigger"} type="button" aria-label={label}
    aria-expanded={assistant.visible} aria-controls="nebula-assistant-panel" title="Nebula assistant" onClick={assistant.toggle}>
    <Bot size={mobile ? 21 : 19} aria-hidden="true" />{mobile && <span>Nebula</span>}
  </button>;
}
