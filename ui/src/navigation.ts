import {
  Bot,
  BookOpen,
  Bug,
  FileSearch,
  FileText,
  LayoutDashboard,
  Network,
  Settings,
  SquareTerminal,
  type LucideIcon,
} from "lucide-react";

export interface NavigationItem {
  commandId: string;
  path: string;
  label: string;
  legacyLabel?: string;
  aliases: string[];
  description: string;
  icon: LucideIcon;
  shortcut: string;
  group: "workspace" | "library" | "settings";
}

export const navigationItems: NavigationItem[] = [
  {
    commandId: "navigate.home",
    path: "/",
    label: "Home",
    legacyLabel: "Overview",
    aliases: ["Overview", "Dashboard"],
    description: "Engagement health, coverage, and mission progress",
    icon: LayoutDashboard,
    shortcut: "G O",
    group: "workspace",
  },
  {
    commandId: "navigate.sessions",
    path: "/sessions",
    label: "Sessions",
    aliases: ["Chat", "Terminal", "Conversations"],
    description: "Human terminals and durable analyst chat",
    icon: SquareTerminal,
    shortcut: "G S",
    group: "workspace",
  },
  {
    commandId: "navigate.missions",
    path: "/agents",
    label: "Missions",
    legacyLabel: "Agents",
    aliases: ["Agents", "Runs"],
    description: "Mission status and persisted activity",
    icon: Bot,
    shortcut: "G A",
    group: "workspace",
  },
  {
    commandId: "navigate.assets",
    path: "/assets",
    label: "Assets",
    aliases: ["Inventory", "Attack surface"],
    description: "Scoped asset records and inventory",
    icon: Network,
    shortcut: "G T",
    group: "workspace",
  },
  {
    commandId: "navigate.findings",
    path: "/findings",
    label: "Findings",
    aliases: ["Vulnerabilities", "Issues"],
    description: "Finding records, evidence links, and lifecycle",
    icon: Bug,
    shortcut: "G F",
    group: "workspace",
  },
  {
    commandId: "navigate.evidence",
    path: "/evidence",
    label: "Evidence",
    aliases: ["Artifacts", "Provenance"],
    description: "Immutable artifacts and provenance",
    icon: FileSearch,
    shortcut: "G E",
    group: "library",
  },
  {
    commandId: "navigate.knowledge",
    path: "/knowledge",
    label: "Knowledge",
    aliases: ["Sources", "Retrieval"],
    description: "Engagement sources, citations, and indexes",
    icon: BookOpen,
    shortcut: "G K",
    group: "library",
  },
  {
    commandId: "navigate.reports",
    path: "/reports",
    label: "Reports",
    aliases: ["Deliverables", "Documents"],
    description: "Executive and technical deliverables",
    icon: FileText,
    shortcut: "G R",
    group: "library",
  },
  {
    commandId: "navigate.settings",
    path: "/settings",
    label: "Settings",
    aliases: ["Preferences", "Configuration"],
    description: "Providers, local attribution, runtime, and appearance",
    icon: Settings,
    shortcut: "G ,",
    group: "settings",
  },
];

export const navigationGroups = [
  { id: "workspace" as const, label: "Workspace" },
  { id: "library" as const, label: "Library" },
];
