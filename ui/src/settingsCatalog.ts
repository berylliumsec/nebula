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
    id: "settings.butler-instructions",
    label: "Nebula-wide Butler instructions",
    description: "Open Nebula-wide BUTLER.md directly in the Markdown code editor, or upload a replacement",
    category: "Automation",
    scope: "Workspace",
    target: "butler-instructions-settings",
    keywords: ["butler", "butler.md", ".butler", "upload", "edit", "scheduled", "act", "projects", "goals", "blocked goal", "check-in conversation", "safe handoff", "provider continuation", "deepseek"],
  },
  {
    id: "settings.butler-check-interval",
    label: "Butler check interval",
    description: "Choose how often Butler checks work across projects; five minutes by default",
    category: "Automation",
    scope: "Workspace",
    target: "butler-check-interval-settings",
    keywords: ["butler", "check", "interval", "frequency", "schedule", "scheduled", "5 minutes", "five minutes", "idle projects", "periodic"],
  },
  {
    id: "settings.butler-effort",
    label: "Butler model and effort",
    description: "Choose Butler's model and reasoning effort for direct messages and scheduled checks",
    category: "Models",
    scope: "Workspace",
    target: "butler-settings",
    keywords: ["butler", "model", "effort", "reasoning", "max", "scheduled", "background"],
  },
  {
    id: "settings.butler-activity",
    label: "Butler scheduled activity",
    description: "Read Butler's saved scheduled-check conversations and linked recovery reports in Oversight",
    category: "Automation",
    scope: "Project",
    target: "butler-oversight",
    keywords: ["butler", "scheduled", "checks", "transcript", "history", "blocked goal", "check-in", "agent reply", "conversation", "recovery goals", "oversight", "idle goals", "goal budget", "step budget", "assistant settings"],
  },
  {
    id: "settings.butler-voice",
    label: "Butler Voice timeout",
    description: "Pause paid Voice sessions after quiet time; open the compact control in Butler settings",
    category: "Models",
    scope: "Workspace",
    target: "butler-settings",
    keywords: ["butler", "voice", "gpt-live", "pause", "mute", "timeout", "cost", "seconds"],
  },
  {
    id: "settings.usage",
    label: "Usage and credits",
    description: "See Codex and Grok credits separately from included usage, plus other balances and Nebula activity",
    category: "Models",
    scope: "Workspace",
    target: "usage-settings",
    keywords: ["usage", "credits", "balance", "remaining", "quota", "rate limit", "tokens", "provider", "harness", "codex", "grok", "grok build", "openrouter", "deepseek"],
  },
  {
    id: "settings.appearance",
    label: "Appearance and theme",
    description: "Choose a color theme for this device",
    category: "Identity & Security",
    scope: "Device",
    target: "appearance-settings",
    keywords: ["appearance", "theme", "light", "dark", "studio dark", "zero light", "zero dark", "zero classic", "zero theme", "glacier", "amber", "color scheme", "display"],
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
    id: "settings.storage",
    label: "Storage sources",
    description: "Connect storage systems, reporting intervals, webhook tokens, and optional callbacks",
    category: "Integrations",
    scope: "Workspace",
    target: "storage-integration-settings",
    keywords: ["storage", "storage integration", "storage source", "webhook", "capacity", "volume", "NAS", "disk alert", "low storage"],
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
    description: "Configure assistant harnesses and optional task plans. To switch one saved harness conversation to a provider, open that conversation's Assistant settings and choose Provider when it is idle",
    category: "Automation",
    scope: "Workspace",
    target: "harness-settings",
    keywords: ["harness", "codex", "grok", "provider", "assistant", "steer", "interrupt", "capabilities", "plan before acting", "ask for a plan", "advisory plan", "share plan", "main agent planning", "subagent plan", "plan only", "safe handoff", "provider goal", "main agent switch"],
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
    keywords: ["project scope", "host mode", "docker mode", "execution mode", "backup", "network", "scope", "domain", "cidr", "port", "url", "allowlist", "all targets", "approval", "privacy", "timeout", "prohibited actions", "always loaded tools", "pinned tools", "on demand tools", "mcp tools", "notebook", "notebook host consent", "notebook package downloads", "notebook package installation", "restart notebook kernel", "notebook file index", "connect notebook index", "remove notebook index"],
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
    description: "Pair a companion browser with a reachable Core address, inspect devices, or revoke access",
    category: "Identity & Security",
    scope: "Security",
    target: "device-pairing-settings",
    keywords: ["device", "pair", "phone", "mobile", "browser", "qr", "revoke", "companion", "lan", "pairing link"],
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
