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
    description: "Human terminals, agent sessions, and chat",
    icon: SquareTerminal,
    shortcut: "G S",
  },
  {
    path: "/agents",
    label: "Agents",
    description: "Mission plan, specialists, and budgets",
    icon: Bot,
    shortcut: "G A",
  },
  {
    path: "/assets",
    label: "Assets",
    description: "Scoped systems, services, and topology",
    icon: Network,
    shortcut: "G T",
  },
  {
    path: "/findings",
    label: "Findings",
    description: "Triage, correlation, and retest lifecycle",
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
    description: "Providers, runners, access, and appearance",
    icon: Settings,
    shortcut: "G ,",
  },
];
