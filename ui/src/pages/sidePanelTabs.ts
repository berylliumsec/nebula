export const SIDE_TOOL_KINDS = ["assistance", "terminal", "code", "browser", "notes", "notebook", "details"] as const;
export type SideToolKind = typeof SIDE_TOOL_KINDS[number];
export interface SideToolTab { id: string; kind: SideToolKind; number: number }

const MAX_TOOL_TABS = 24;
const ID_PATTERN = /^[a-zA-Z0-9-]{1,64}$/;

export function sideToolTabs(params: URLSearchParams): SideToolTab[] {
  const seen = new Set<string>();
  return params.getAll("panel").slice(0, MAX_TOOL_TABS).flatMap(value => {
    const [kind, numberText, id, extra] = value.split(":");
    const number = Number(numberText);
    if (extra || !SIDE_TOOL_KINDS.includes(kind as SideToolKind) || !Number.isSafeInteger(number) || number < 1 || !ID_PATTERN.test(id ?? "") || seen.has(id)) return [];
    seen.add(id);
    return [{id, kind: kind as SideToolKind, number}];
  });
}

export function appendSideToolTab(params: URLSearchParams, kind: SideToolKind, id: string): SideToolTab | undefined {
  if (!ID_PATTERN.test(id)) return undefined;
  const tabs = sideToolTabs(params);
  if (tabs.length >= MAX_TOOL_TABS) return undefined;
  const tab = {id, kind, number: Math.max(0, ...tabs.filter(item => item.kind === kind).map(item => item.number)) + 1};
  params.append("panel", `${kind}:${tab.number}:${id}`);
  params.set("panelTab", id);
  params.delete("side");
  params.delete("companion");
  params.set("view", "chat");
  return tab;
}

export function removeSideToolTab(params: URLSearchParams, id: string): void {
  const remaining = sideToolTabs(params).filter(tab => tab.id !== id);
  params.delete("panel");
  for (const tab of remaining) params.append("panel", `${tab.kind}:${tab.number}:${tab.id}`);
  params.delete(`panelFile.${id}`);
  params.delete(`panelLine.${id}`);
  params.delete(`panelUrl.${id}`);
  if (params.get("panelTab") === id) {
    if (remaining.length) params.set("panelTab", remaining.at(-1)!.id);
    else params.delete("panelTab");
  }
}

export function clearSideToolTabs(params: URLSearchParams): void {
  for (const tab of sideToolTabs(params)) {
    params.delete(`panelFile.${tab.id}`);
    params.delete(`panelLine.${tab.id}`);
    params.delete(`panelUrl.${tab.id}`);
  }
  params.delete("panel");
  params.delete("panelTab");
}

export function sideToolLabel(tab: SideToolTab): string {
  const names: Record<SideToolKind, string> = {
    assistance: "Tool assistance", terminal: "Terminal", code: "Code", browser: "Browser",
    notes: "Notes", notebook: "Notebook", details: "Chat details",
  };
  return `${names[tab.kind]} ${tab.number}`;
}
