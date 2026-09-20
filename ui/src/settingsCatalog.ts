export type SettingScope = "Device" | "Project" | "Workspace" | "Security";

export interface SettingCatalogEntry {
  id: string;
  label: string;
  description: string;
  category: string;
  scope: SettingScope;
  target: string;
  keywords: string[];
}

export const settingCatalog: SettingCatalogEntry[] = [
  {
    id: "settings.appearance",
    label: "Appearance and theme",
    description: "Choose Light, Dark, or Zero Dark for this device",
    category: "Identity & Security",
    scope: "Device",
    target: "appearance-settings",
    keywords: ["appearance", "theme", "light", "dark", "zero dark", "color scheme", "display"],
  },
  {
    id: "settings.providers",
    label: "Model providers",
    description: "Add providers, credentials, discovered models, and capability limits",
    category: "Models",
    scope: "Workspace",
    target: "provider-settings",
    keywords: ["provider", "model", "openai", "anthropic", "gemini", "ollama", "vllm", "credential", "api key"],
  },
  {
    id: "settings.skills",
    label: "Shared skills",
    description: "Review the .agents/skills catalogs assistants can call with $",
    category: "Models",
    scope: "Project",
    target: "native-skill-settings",
    keywords: ["skill", "skills", "skill.md", ".agents", "agents skills", "shared skills", "playbook", "procedure", "catalog"],
  },
  {
    id: "settings.web-search",
    label: "Web search runtime",
    description: "Run the local metasearch container agents use to research the public web",
    category: "Integrations",
    scope: "Workspace",
    target: "web-search-runtime-settings",
    keywords: ["web search", "search", "internet", "research", "searxng", "metasearch", "google", "duckduckgo", "brave", "online"],
  },
  {
    id: "settings.follow-up",
    label: "Post-tool assistant",
    description: "Configure automatic follow-up after terminal and tool results",
    category: "Automation",
    scope: "Project",
    target: "post-tool-assistant-settings",
    keywords: ["post tool", "follow up", "assistant", "automatic", "suggestion"],
  },
  {
    id: "settings.harnesses",
    label: "Assistant harnesses",
    description: "Configure Codex and other assistant harness capabilities",
    category: "Automation",
    scope: "Workspace",
    target: "harness-settings",
    keywords: ["harness", "codex", "assistant", "steer", "interrupt", "capabilities"],
  },
  {
    id: "settings.mcp",
    label: "MCP servers",
    description: "Manage trusted local and HTTPS MCP integrations",
    category: "Automation",
    scope: "Security",
    target: "mcp-settings",
    keywords: ["mcp", "server", "tool", "integration", "stdio", "streamable http"],
  },
  {
    id: "settings.ssh-environments",
    label: "Environments",
    description: "Let agents run commands on machines from ~/.ssh/config",
    category: "Environments",
    scope: "Security",
    target: "ssh-environment-settings",
    keywords: ["environment", "ssh", "remote", "host", "mac", "linux", "machine", "ssh config", "server"],
  },
  {
    id: "settings.automation-runtime",
    label: "Automation runtime",
    description: "Select the command runtime used by automated operations",
    category: "Automation",
    scope: "Workspace",
    target: "automation-runtime-settings",
    keywords: ["automation", "runtime", "command", "container", "execution"],
  },
  {
    id: "settings.vpn-egress",
    label: "VPN egress profiles",
    description: "Upload OpenVPN profiles and route project command containers through a tunnel",
    category: "Automation",
    scope: "Security",
    target: "automation-runtime-settings",
    keywords: ["vpn", "openvpn", "ovpn", "tunnel", "egress", "profile", "kill switch"],
  },
  {
    id: "settings.runners",
    label: "Sandbox runners",
    description: "Configure trusted Docker or Podman execution profiles",
    category: "Automation",
    scope: "Workspace",
    target: "runtime-settings",
    keywords: ["runner", "sandbox", "docker", "podman", "socket", "seccomp", "container"],
  },
  {
    id: "settings.network-scope",
    label: "Project policy and network scope",
    description: "Control targets, ports, URLs, approvals, privacy, and execution limits",
    category: "Project Policy",
    scope: "Project",
    target: "engagement-policy-settings",
    keywords: ["host mode", "docker mode", "execution mode", "backup", "network", "scope", "domain", "cidr", "port", "url", "allowlist", "all targets", "approval", "privacy", "timeout", "prohibited actions", "always loaded tools", "pinned tools", "on demand tools", "mcp tools"],
  },
  {
    id: "settings.operators",
    label: "Operator profiles",
    description: "Manage the local identity used for activity attribution",
    category: "Identity & Security",
    scope: "Security",
    target: "operator-settings",
    keywords: ["operator", "profile", "identity", "name", "email", "role", "attribution"],
  },
  {
    id: "settings.devices",
    label: "Paired devices",
    description: "Pair, inspect, and revoke browsers and mobile devices",
    category: "Identity & Security",
    scope: "Security",
    target: "device-pairing-settings",
    keywords: ["device", "pair", "phone", "mobile", "browser", "qr", "revoke"],
  },
  {
    id: "settings.release",
    label: "Release and updates",
    description: "Inspect the installed version and update channel",
    category: "Release",
    scope: "Device",
    target: "release-settings-group",
    keywords: ["release", "update", "version", "channel", "upgrade"],
  },
  {
    id: "settings.diagnostics",
    label: "Diagnostics and logging",
    description: "Configure logs and inspect recoverable interface failures",
    category: "Diagnostics",
    scope: "Device",
    target: "diagnostics-settings",
    keywords: ["diagnostics", "logging", "logs", "errors", "support bundle", "telemetry"],
  },
  {
    id: "settings.setup",
    label: "Terminal and Core setup",
    description: "Check runtime readiness and configure the UI shell connection",
    category: "Setup",
    scope: "Workspace",
    target: "setup-settings",
    keywords: ["setup", "terminal", "core", "remote core", "connection", "readiness", "kali"],
  },
];

export function settingCatalogText(entry: SettingCatalogEntry): string {
  return [entry.label, entry.description, entry.category, entry.scope, ...entry.keywords].join(" ");
}

export function settingCatalogEntry(id: string): SettingCatalogEntry {
  const entry = settingCatalog.find((item) => item.id === id);
  if (!entry) throw new Error(`Unknown setting catalog entry: ${id}`);
  return entry;
}
