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

export function settingOpensInButler(entry: SettingCatalogEntry): boolean {
  return entry.target === "butler-settings" || entry.target === "butler-oversight";
}

export const settingCatalog: SettingCatalogEntry[] = [
  {
    id: "settings.memory-budgets",
    label: "Memory budgets",
    description: "Set job memory, swap and total capacity; reserve Core and runtime memory; inspect enforced limits and usage",
    category: "Automation",
    scope: "Workspace",
    target: "memory-budget-settings",
    keywords: ["memory", "ram", "gib", "worker", "budget", "out of memory", "oom", "limits", "swap", "reserve", "compute", "pressure", "butler"],
  },
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
    description: "Read concurrent Butler checks, investigation transcripts and subagent results, and review held improvement drafts in Oversight",
    category: "Automation",
    scope: "Project",
    target: "butler-oversight",
    keywords: ["butler", "scheduled", "checks", "transcript", "history", "blocked goal", "check-in", "agent reply", "conversation", "recovery goals", "oversight", "idle goals", "goal budget", "step budget", "assistant settings", "improvement issues", "held draft", "issue destination", "repository review", "parallel", "concurrent", "subagents", "background workers"],
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
    keywords: ["provider", "model", "models", "openai", "anthropic", "gemini", "ollama", "vllm", "credential", "api key", "api keys"],
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
    description: "Install, start, stop and refresh local Web search and choose its engines",
    category: "Integrations",
    scope: "Workspace",
    target: "web-search-runtime-settings",
    keywords: ["web search", "search", "internet", "research", "searxng", "metasearch", "google", "duckduckgo", "brave", "online"],
  },
  {
    id: "settings.storage",
    label: "Storage sources",
    description: "Manage storage sources and review source-proposed plans, reconciliation, and failed dispatch retries",
    category: "Integrations",
    scope: "Workspace",
    target: "storage-integration-settings",
    keywords: ["storage", "storage integration", "storage source", "enroll source", "rotate token", "revoke token", "reporting interval", "callback settings", "webhook", "capacity", "volume", "NAS", "disk alert", "low storage", "source action", "preview effects", "approve plan", "reconcile action", "retry dispatch"],
  },
  {
    id: "settings.typesafe",
    label: "TypeSafe API key",
    description: "Enter, replace or remove the tool-suggestions key on this device",
    category: "Integrations",
    scope: "Security",
    target: "typesafe-integration-settings",
    keywords: ["typesafe", "jev", "tool suggestions", "typesafe api key", "integration key"],
  },
  {
    id: "settings.follow-up",
    label: "Post-tool assistant",
    description: "Choose automatic follow-up analysis and project cloud consent; manual note drafts require operator acceptance",
    category: "Automation",
    scope: "Project",
    target: "post-tool-assistant-settings",
    keywords: ["post tool", "follow up", "assistant", "automatic", "suggestion", "manual draft", "execution note", "operator acceptance"],
  },
  {
    id: "settings.harnesses",
    label: "Assistant harnesses",
    description: "Configure assistant harnesses, independent provider or harness subagents, project lifecycle guards and optional task plans. Core goals follow the selected runtime in the same chat at a safe turn boundary; paused and blocked goals keep their state",
    category: "Automation",
    scope: "Workspace",
    target: "harness-settings",
    keywords: ["harness", "codex", "grok", "provider", "assistant", "steer", "interrupt", "capabilities", "plan before acting", "ask for a plan", "advisory plan", "share plan", "main agent planning", "subagent plan", "mixed runtimes", "lifecycle hooks", "guarded workers", "plan only", "safe handoff", "provider goal", "main agent switch", "pending runtime switch", "harness to provider", "goal resume", "test turn", "external session", "import session history"],
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
    description: "Manage SSH host metadata; grant standing command approval in this browser",
    category: "Environments",
    scope: "Security",
    target: "ssh-environment-settings",
    keywords: ["environment", "ssh", "remote", "host", "mac", "linux", "machine", "ssh config", "server"],
  },
  {
    id: "settings.automation-runtime",
    label: "Automation runtime",
    description: "Inspect and prepare the shared automation image; project policy chooses the command runtime",
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
    description: "Review trusted Docker or Podman programs and isolation; Butler can rename existing profiles",
    category: "Automation",
    scope: "Workspace",
    target: "runtime-settings",
    keywords: ["runner", "sandbox", "docker", "podman", "socket", "seccomp", "container"],
  },
  {
    id: "settings.network-scope",
    label: "Project policy and network scope",
    description: "Control targets, ports, URLs, approvals and limits, or review targets extracted from a private scope document",
    category: "Project Policy",
    scope: "Project",
    target: "engagement-policy-settings",
    keywords: ["project scope", "host mode", "docker mode", "execution mode", "backup", "network", "scope", "domain", "cidr", "port", "url", "allowlist", "all targets", "approval", "privacy", "timeout", "prohibited actions", "always loaded tools", "pinned tools", "on demand tools", "mcp tools", "notebook", "notebook host consent", "notebook package downloads", "notebook package installation", "restart notebook kernel", "notebook file index", "connect notebook index", "remove notebook index", "scope import", "scope document", "review targets", "discard import"],
  },
  {
    id: "settings.operators",
    label: "Operator profiles",
    description: "Choose and manage local activity attribution profiles",
    category: "Identity & Security",
    scope: "Security",
    target: "operator-settings",
    keywords: ["operator", "profile", "identity", "name", "email", "role", "attribution"],
  },
  {
    id: "settings.devices",
    label: "Paired devices",
    description: "Pair, inspect, and revoke browsers, mobile devices, and terminal Assistant clients",
    category: "Identity & Security",
    scope: "Security",
    target: "device-pairing-settings",
    keywords: ["device", "pair", "pairing link", "unpair", "phone", "mobile", "browser", "qr", "revoke", "CLI", "terminal", "nebula-chat", "nebula-client", "Assistant shell", "project snapshot", "/snapshot", "F11 snapshot", "project notes", "terminal notes", "/notes", "notes editor", "up arrow message recall", "composer history", "last sent message", "remote agent chats", "running conversation", "waiting conversation", "idle conversation", "turn status", "terminal loading", "animated spinner", "selection feedback", "reduced motion", "multi-select skills", "/skills", "/goal skills", "MCP server picker", "/mcp", "headless"],
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
    description: "Configure logs, inspect failures, and review sensitive detail or remediation actions in the browser",
    category: "Diagnostics",
    scope: "Device",
    target: "diagnostics-settings",
    keywords: ["diagnostics", "logging", "logs", "errors", "support bundle", "telemetry", "performance", "bottlenecks", "latency", "SQL timing", "worker queue", "event loop", "PostgreSQL", "SQLite", "database migration", "migrate-to-postgresql", "public incidents", "Core health", "sensitive detail", "reveal detail", "copy detail", "remediation", "recovery action", "log level"],
  },
  {
    id: "settings.host-pressure",
    label: "Host pressure admission",
    description: "Set live CPU, memory, runnable-load and Core I/O thresholds for new starts, and read host telemetry",
    category: "Setup",
    scope: "Workspace",
    target: "setup-settings",
    keywords: ["host pressure", "threshold", "CPU", "memory", "runnable tasks", "load average", "Core I/O PSI", "backup disk", "some avg10", "80%", "queued", "admission", "nebula.work.host_pressure", "nebula.global.host_pressure"],
  },
  {
    id: "settings.setup",
    label: "Terminal and Core setup",
    description: "Choose a verified runtime, enable background PDF workers with this host's CPU budget, or inspect host pressure telemetry",
    category: "Setup",
    scope: "Workspace",
    target: "setup-settings",
    keywords: ["setup", "terminal", "terminal TUI", "full-screen chat", "live agent progress", "nebula-chat", "core", "remote core", "connection", "readiness", "kali", "host load", "host-load", "CPU", "memory", "capacity", "fresh host load", "I/O pressure", "load average", "sample age", "host telemetry", "CPU budget", "per host", "automatic", "custom CPU limit", "applied quota", "enable background PDF workers", "disable workers", "Linux CPU affinity", "draining", "worker activation", "nebula.work.host_load", "nebula.global.host_load", "nebula.work.host_cpu_budget", "nebula.global.host_cpu_budget"],
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
