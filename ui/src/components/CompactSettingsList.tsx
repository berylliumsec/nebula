import { useMemo, useState } from "react";
import {
  Activity,
  BookOpen,
  Bot,
  Boxes,
  ChevronRight,
  Cpu,
  Globe2,
  Link2,
  Moon,
  Plug,
  RefreshCw,
  Search,
  Server,
  ShieldCheck,
  Smartphone,
  Sparkles,
  SquareTerminal,
  UserRound,
  X,
  type LucideIcon,
} from "lucide-react";
import { settingCatalog, settingCatalogText, type SettingCatalogEntry } from "../settingsCatalog";
import { useOptionalChrome } from "../state/ChromeContext";
import { isNebulaShell } from "../hooks/useCompactLayout";

const categoryOrder = ["Setup", "Models", "Automation", "Environments", "Project Policy", "Identity & Security", "Release", "Diagnostics"];

/** Row glyph per catalog entry; every entry needs one so no row falls back to the generic terminal. */
export const settingIcons: Record<string, LucideIcon> = {
  "settings.setup": SquareTerminal,
  "settings.providers": Bot,
  "settings.skills": BookOpen,
  "settings.follow-up": Sparkles,
  "settings.harnesses": Cpu,
  "settings.mcp": Plug,
  "settings.ssh-environments": Server,
  "settings.automation-runtime": Boxes,
  "settings.vpn-egress": ShieldCheck,
  "settings.runners": Boxes,
  "settings.network-scope": Globe2,
  "settings.operators": UserRound,
  "settings.devices": Smartphone,
  "settings.appearance": Moon,
  "settings.release": RefreshCw,
  "settings.diagnostics": Activity,
};

/** Groups catalog entries in display order, keeping unknown categories last. */
export function groupSettings(entries: SettingCatalogEntry[]): Array<{ category: string; entries: SettingCatalogEntry[] }> {
  const groups = new Map<string, SettingCatalogEntry[]>();
  for (const entry of entries) groups.set(entry.category, [...(groups.get(entry.category) ?? []), entry]);
  const rank = (category: string) => {
    const index = categoryOrder.indexOf(category);
    return index === -1 ? categoryOrder.length : index;
  };
  return [...groups].sort(([a], [b]) => rank(a) - rank(b)).map(([category, grouped]) => ({ category, entries: grouped }));
}

/** Phone Settings: one searchable grouped list; each row opens its focused lens. */
export function CompactSettingsList() {
  const chrome = useOptionalChrome();
  const [query, setQuery] = useState("");
  const groups = useMemo(() => {
    const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const matching = terms.length
      ? settingCatalog.filter((entry) => terms.every((term) => settingCatalogText(entry).toLowerCase().includes(term)))
      : settingCatalog;
    return groupSettings(matching);
  }, [query]);
  const shell = isNebulaShell();
  return <div className="compact-settings">
    <label className="compact-settings-search">
      <Search size={16} aria-hidden="true" />
      <input type="search" aria-label="Search settings" placeholder="Search settings" value={query} onChange={(event) => setQuery(event.target.value)} />
      {query && <button className="icon-button subtle" type="button" aria-label="Clear settings search" onClick={() => setQuery("")}><X size={15} aria-hidden="true" /></button>}
    </label>
    {groups.map(({ category, entries }) => <section className="compact-settings-group" key={category} aria-labelledby={`compact-settings-${category.replace(/\W+/g, "-").toLowerCase()}`}>
      <h2 id={`compact-settings-${category.replace(/\W+/g, "-").toLowerCase()}`}>{category}</h2>
      <div>
        {entries.map((entry) => {
          const Icon = settingIcons[entry.id] ?? SquareTerminal;
          return <button type="button" key={entry.id} disabled={!chrome?.openSetting} onClick={(event) => chrome?.openSetting?.(entry, event.currentTarget)}>
            <span className="compact-settings-icon" aria-hidden="true"><Icon size={16} /></span>
            <span className="compact-settings-label"><strong>{entry.label}</strong><small>{entry.description}</small></span>
            <ChevronRight size={16} aria-hidden="true" />
          </button>;
        })}
      </div>
    </section>)}
    {!groups.length && <p className="compact-settings-empty" role="status">No settings match “{query}”.</p>}
    {shell && !query && <section className="compact-settings-group" aria-labelledby="compact-settings-this-iphone">
      <h2 id="compact-settings-this-iphone">This iPhone</h2>
      <div>
        <a href="nebula://settings">
          <span className="compact-settings-icon" aria-hidden="true"><Link2 size={16} /></span>
          <span className="compact-settings-label"><strong>App connection</strong><small>{window.location.host} · server address, pairing, reload</small></span>
          <ChevronRight size={16} aria-hidden="true" />
        </a>
      </div>
    </section>}
  </div>;
}
