import {
  Bug,
  BookMarked,
  FileText,
  FolderKanban,
  Settings,
  PanelsTopLeft,
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
  group: "workspace" | "settings";
}

export const navigationItems: NavigationItem[] = [
  {
    commandId: "navigate.workbench",
    path: "/",
    label: "Workbench",
    legacyLabel: "Sessions",
    aliases: ["Sessions", "Chat", "Terminal", "Files", "Activity"],
    description: "Terminal, assistant, files, and activity",
    icon: PanelsTopLeft,
    shortcut: "G O",
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
    commandId: "navigate.reports",
    path: "/reports",
    label: "Reports",
    aliases: ["Deliverables", "Documents"],
    description: "Executive and technical deliverables",
    icon: FileText,
    shortcut: "G R",
    group: "workspace",
  },
  {
    commandId: "navigate.project",
    path: "/project",
    label: "Project",
    legacyLabel: "Engagement",
    aliases: ["Overview", "Assets", "Evidence", "Knowledge", "Sources", "Engagement"],
    description: "Project overview, assets, evidence, and sources",
    icon: FolderKanban,
    shortcut: "G P",
    group: "workspace",
  },
  {
    commandId: "navigate.library",
    path: "/library",
    label: "Library",
    aliases: ["Knowledge base", "Repository", "Documents", "Scripts", "Chroma"],
    description: "Reusable documents and scripts shared across projects",
    icon: BookMarked,
    shortcut: "G L",
    group: "workspace",
  },
  {
    commandId: "navigate.settings",
    path: "/settings",
    label: "Settings",
    aliases: ["Preferences", "Configuration", "Providers", "Models", "Runners", "Policy", "Privacy"],
    description: "Simple setup and advanced configuration",
    icon: Settings,
    shortcut: "G ,",
    group: "settings",
  },
];

// Canonical project surfaces and the navigation item that owns each one.
const projectSurfaceItemPaths: Record<string, string> = {
  workbench: "/",
  findings: "/findings",
  reports: "/reports",
  assets: "/project",
  evidence: "/project",
  sources: "/project",
};

// Legacy top-level routes that render another item's page when no project is loaded.
const legacySurfaceItemPaths: Record<string, string> = {
  assets: "/project",
  evidence: "/project",
  knowledge: "/project",
};

/**
 * Resolves legacy and canonical `/projects/<id>/<surface>/<resource>` routes to their navigation item.
 * This is the single authority for the current page in the top bar and side navigation.
 */
export function navigationItemForPath(pathname: string): NavigationItem {
  const project = /^\/projects\/[^/]+(?:\/([^/]+))?/.exec(pathname);
  const segment = pathname.split("/")[1] ?? "";
  const path = project
    ? project[1] ? projectSurfaceItemPaths[project[1]] : "/project"
    : legacySurfaceItemPaths[segment] ?? `/${segment}`;
  return navigationItems.find((item) => item.path === path) ?? navigationItems[0];
}

export const navigationGroups = [
  { id: "workspace" as const, label: "Workspace" },
];
