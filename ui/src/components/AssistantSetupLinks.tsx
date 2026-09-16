import { ChevronRight, type LucideIcon } from "lucide-react";
import type { SettingCatalogEntry } from "../settingsCatalog";

export interface AssistantSetupLink {
  detail: string;
  entry: SettingCatalogEntry;
  icon: LucideIcon;
  label: string;
}

interface AssistantSetupLinksProps {
  items: AssistantSetupLink[];
  onOpen(entry: SettingCatalogEntry): void;
}

export function AssistantSetupLinks({ items, onOpen }: AssistantSetupLinksProps) {
  return (
    <section className="chat-settings-setup" aria-labelledby="assistant-setup-title">
      <div className="chat-settings-section-heading"><strong id="assistant-setup-title">Assistant setup</strong><small>Workspace and project defaults</small></div>
      <div className="chat-settings-setup-list">
        {items.map(({ detail, entry, icon: Icon, label }) => <button aria-label={`${label}, ${detail}`} className="chat-settings-setup-row" type="button" key={entry.id} onClick={() => onOpen(entry)}>
          <span className="chat-settings-setup-icon"><Icon size={16} aria-hidden="true" /></span>
          <span><strong>{label}</strong><small>{detail}</small></span>
          <ChevronRight size={15} aria-hidden="true" />
        </button>)}
      </div>
    </section>
  );
}
