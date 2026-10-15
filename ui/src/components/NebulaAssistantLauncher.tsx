import { createContext, useContext } from "react";
import styles from "./NebulaAssistantLauncher.module.css";

export interface NebulaAssistantLauncherState {
  opened: boolean;
  visible: boolean;
  attentionCount?: number;
  toggle: () => void;
}
export const NebulaAssistantLauncherContext = createContext<NebulaAssistantLauncherState | null>(null);

export function NebulaAssistantLauncherButton({ mobile = false }: { mobile?: boolean }) {
  const assistant = useContext(NebulaAssistantLauncherContext);
  if (!assistant) return null;
  const label = assistant.visible ? "Toggle Butler" : assistant.opened ? "Show Butler" : "Open Butler";
  const attentionCount = assistant.attentionCount ?? 0;
  const attentionLabel = `${attentionCount} open incident${attentionCount === 1 ? "" : "s"} ${attentionCount === 1 ? "needs" : "need"} attention in the selected project`;
  return <button className={`${mobile ? styles.mobile : "icon-button toolbar-button nebula-assistant-trigger"} ${styles.button}`} type="button" aria-label={attentionCount ? `${label}; ${attentionLabel}` : label}
    aria-expanded={assistant.visible} aria-controls="nebula-assistant-panel" title={attentionCount ? attentionLabel : "Butler"} onClick={assistant.toggle}>
    <img className="nebula-assistant-mark" src="/nebula-logo.svg" width="18" height="29" alt="" aria-hidden="true" />{mobile && <span>Butler</span>}
    {attentionCount > 0 && <i className={styles.attention} aria-hidden="true" />}
  </button>;
}
