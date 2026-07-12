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
  path: string;
  label: string;
  description: string;
  icon: LucideIcon;
  shortcut: string;
}

export const navigationItems: NavigationItem[] = [
  {
    path: "/",
    label: "Overview",
    description: "Engagement health, coverage, and mission progress",
    icon: LayoutDashboard,
    shortcut: "G O",
  },
  {
    path: "/sessions",
    label: "Sessions",
    description: "Human terminals and durable analyst chat",
    icon: SquareTerminal,
    shortcut: "G S",
  },
  {
    path: "/agents",
    label: "Agents",
    description: "Mission status and persisted activity",
    icon: Bot,
    shortcut: "G A",
  },
  {
    path: "/assets",
    label: "Assets",
    description: "Scoped asset records and inventory",
    icon: Network,
    shortcut: "G T",
  },
  {
    path: "/findings",
    label: "Findings",
    description: "Finding records, evidence links, and lifecycle",
    icon: Bug,
    shortcut: "G F",
  },
  {
    path: "/evidence",
    label: "Evidence",
    description: "Immutable artifacts and provenance",
    icon: FileSearch,
    shortcut: "G E",
  },
  {
    path: "/knowledge",
    label: "Knowledge",
    description: "Engagement sources, citations, and indexes",
    icon: BookOpen,
    shortcut: "G K",
  },
  {
    path: "/reports",
    label: "Reports",
    description: "Executive and technical deliverables",
    icon: FileText,
    shortcut: "G R",
  },
  {
    path: "/settings",
    label: "Settings",
    description: "Providers, local attribution, runtime, and appearance",
    icon: Settings,
    shortcut: "G ,",
  },
];
