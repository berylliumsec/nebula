import { projectSurface } from "../resourceRoutes";
import type { GuideCheckResult, GuideDefinition, GuideRunContext } from "./types";

function workbench(context: GuideRunContext, parameters: Record<string, string>): string | undefined {
  if (!context.engagementId) return undefined;
  return `${projectSurface(context.engagementId, "workbench")}?${new URLSearchParams(parameters)}`;
}

function failure(error: unknown): GuideCheckResult {
  return { state: "problem", message: error instanceof Error ? error.message : "Nebula Core could not check this step." };
}

function onScreen(selector: string): boolean {
  return document.querySelector(selector) !== null;
}

const hookName = (context: GuideRunContext) => context.values.name || "audit";
const skillName = (context: GuideRunContext) => context.values.name || "recon-checklist";

async function hookListed(context: GuideRunContext): Promise<GuideCheckResult> {
  if (!context.api || !context.engagementId) return { state: "waiting", message: "Open a project to check its hooks." };
  try {
    const hooks = await context.api.listNativeHooks(context.engagementId);
    const hook = hooks.find(item => item.id === hookName(context)) ?? hooks[0];
    return hook
      ? { state: "done", message: `Nebula lists “${hook.manifest.name}” for this project.` }
      : { state: "waiting", message: `Waiting for .agents/hooks/${hookName(context)}/hook.json…` };
  } catch (error) {
    // diagnostic-expected: Core rejects the whole catalog when one manifest is invalid; the step shows its reason.
    return failure(error);
  }
}

async function skillListed(context: GuideRunContext): Promise<GuideCheckResult> {
  if (!context.api || !context.engagementId) return { state: "waiting", message: "Open a project to check its skills." };
  try {
    const skills = await context.api.listSkills(context.engagementId);
    const skill = skills.find(item => item.name === skillName(context));
    return skill
      ? { state: "done", message: `Nebula lists the ${skill.name} skill.` }
      : { state: "waiting", message: `Waiting for .agents/skills/${skillName(context)}/SKILL.md…` };
  } catch (error) {
    // diagnostic-expected: the step shows Core's reason and keeps polling.
    return failure(error);
  }
}

async function instructionsFound(context: GuideRunContext): Promise<GuideCheckResult> {
  if (!context.api || !context.engagementId) return { state: "waiting", message: "Open a project to check AGENTS.md." };
  try {
    const status = await context.api.getProjectInstructionsStatus(context.engagementId);
    if (status.error) return { state: "problem", message: status.error };
    if (!status.present) return { state: "waiting", message: "Waiting for AGENTS.md at the project root…" };
    return status.truncated
      ? { state: "problem", message: `AGENTS.md is ${status.sizeBytes.toLocaleString()} bytes; only the first ${Math.round(status.limitBytes / 1024)} KiB are sent.` }
      : { state: "done", message: `AGENTS.md found (${status.sizeBytes.toLocaleString()} bytes). It is sent with every provider turn.` };
  } catch (error) {
    // diagnostic-expected: the step shows Core's reason and keeps polling.
    return failure(error);
  }
}

export const guideCatalog: GuideDefinition[] = [
  {
    id: "assistant-runtime",
    title: "Choose who answers: a model or a coding harness",
    summary: "Runtime, provider, model and harness options in Assistant settings.",
    category: "assistant",
    pages: ["workbench"],
    keywords: "runtime provider model harness codex grok effort speed mode resume assistant settings",
    steps: [
      {
        title: "Pick the runtime for your next message",
        body: [
          "Provider: Nebula sends the conversation to a model provider you configured and runs every tool itself. Harness: a coding agent such as Codex or Grok runs the turn using its own sign-in.",
          "Harnesses also offer Effort, Speed and Mode when they advertise them, and Resume existing session to pick up an earlier session of that harness. Changes apply to your next message.",
        ],
        route: context => workbench(context, { view: "chat" }),
        action: "open-assistant-settings",
        target: "assistant-runtime",
        requiresProject: true,
      },
      {
        title: "Add a provider or harness",
        body: [
          "Nothing to choose from yet? Add a model provider here. Harnesses are under Settings → Automation → Harnesses.",
        ],
        route: () => "/settings#provider-settings",
        target: "add-provider",
      },
    ],
  },
  {
    id: "bring-context",
    title: "Bring files, pages and selections into the chat",
    summary: "Attach documents and images, or send any selected text to the assistant.",
    category: "assistant",
    pages: ["workbench"],
    keywords: "attach file image document upload paste drop selection context knowledge excerpt ask nebula",
    steps: [
      {
        title: "Attach to your next message",
        body: [
          "Attach files lets you add images or a document from this device, browse the project’s files, or attach an excerpt from a project knowledge source.",
          "You can also paste or drop images straight into the message box.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "attach-files",
        requiresProject: true,
      },
      {
        title: "Send any selection",
        body: [
          "Select text anywhere in the Workbench (terminal output, notes, a web page, an earlier answer) and choose Add context to chat. It is attached to your next message, not sent yet.",
          "Ask Nebula opens a small floating assistant for a quick question about the selection without leaving what you are doing.",
        ],
      },
    ],
  },
  {
    id: "side-terminal",
    title: "Run the assistant’s commands beside the chat",
    summary: "Open the live terminal next to the conversation and send code blocks to it.",
    category: "assistant",
    pages: ["workbench"],
    keywords: "terminal side shell run in terminal command code block split",
    steps: [
      {
        title: "Open the terminal beside the chat",
        body: [
          "This opens the same live shell as the Terminal tab next to the conversation. Drag the divider to resize it; double-click the divider to reset its width.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "terminal-toggle",
        requiresProject: true,
        check: () => onScreen("#chat-side-terminal")
          ? { state: "done", message: "Terminal open beside the chat." }
          : { state: "waiting", message: "Waiting for the terminal to open…" },
      },
      {
        title: "Send a command from an answer",
        body: [
          "Shell code blocks in the assistant’s answers have Run in terminal. It runs the command right away in the terminal beside the chat, where you can watch it and stop it with Ctrl+C.",
        ],
      },
    ],
  },
  {
    id: "command-approvals",
    title: "Decide what the assistant may run",
    summary: "Execution mode, approval policy and the Approval required card.",
    category: "assistant",
    pages: ["workbench", "settings"],
    keywords: "approval approve reject policy docker host mode permission safety commands network scope",
    steps: [
      {
        title: "Set the project’s rules",
        body: [
          "Execution mode: Docker runs commands in a container with the project folder at `/workspace`; Host runs them directly on the Nebula server and needs an explicit acknowledgement.",
          "Approval policy: On boundary prompts once for project networking, Always prompts before every command, Never does not prompt.",
        ],
        route: () => "/settings#engagement-policy-settings",
        target: "project-policy",
        requiresProject: true,
      },
      {
        title: "Answer an approval in the chat",
        body: [
          "When a command needs approval, the answer pauses with an Approval required card showing the exact request. Approve lets that request run, Reject declines it, and Stop response ends the turn.",
          "Waiting approvals also appear behind the shield in the top bar (⌥⌘I).",
        ],
      },
    ],
  },
  {
    id: "queue-and-steer",
    title: "Keep talking while the assistant works",
    summary: "Queue follow-ups, stop and send, or guide a running harness.",
    category: "assistant",
    pages: ["workbench"],
    keywords: "queue follow-up stop send steer guide interrupt busy running later",
    steps: [
      {
        title: "Type while a response runs",
        body: [
          "While an answer is running you can keep typing. Queue follow-up message sends it next; Stop and send interrupts the current answer and sends yours now. Queue for later saves a message without sending it.",
          "Harnesses that support steering show Guide current turn instead, which adds your guidance to the turn already running.",
          "Queued messages wait in the follow-up queue above the message box, where you can edit, reorder, pause or clear them.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "composer",
        requiresProject: true,
      },
    ],
  },
  {
    id: "branch-and-find",
    title: "Branch, fork and find earlier messages",
    summary: "Message actions, bookmarks and conversation search.",
    category: "assistant",
    pages: ["workbench"],
    keywords: "fork branch edit bookmark copy quote search history message actions",
    steps: [
      {
        title: "Act on a message",
        body: [
          "Under each saved message: Bookmark it, Edit (your messages) to change the wording and resend it here, Copy, Quote it into the message box, or Fork conversation here to continue separately. Editing stays in this conversation and replaces the turns below it, which stay readable under “replaced messages”; forking makes a second conversation that shares the project files.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "message-actions",
        targetMissing: "Message actions appear under each saved message. Send a message first.",
        requiresProject: true,
      },
      {
        title: "Search messages and bookmarks",
        body: [
          "Search finds text across this conversation and lists your bookmarks, so you can jump straight back to a result.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "transcript-search",
        requiresProject: true,
      },
    ],
  },
  {
    id: "conversation-goals",
    title: "Let the assistant work toward a goal",
    summary: "Objectives with completion criteria, budgets and a verified finish.",
    category: "assistant",
    pages: ["workbench"],
    keywords: "goal objective budget criteria plan autonomous continue complete /goal usage",
    steps: [
      {
        title: "Add a goal to a provider conversation",
        body: [
          "Add goal takes an objective, completion criteria (one per line) and an optional plan. Limits set token, time, step and child-goal budgets.",
          "Start, Pause, Block and Complete control it. Completing asks for a summary and the evidence you verified.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "goal-panel",
        targetMissing: "Goals appear above the message box in a saved provider conversation. Send a first message with a provider model.",
        requiresProject: true,
      },
      {
        title: "Goals with a harness",
        body: [
          "With Codex or Grok, type `/goal` and your objective in the message box. `/goals` lists goals and `/usage` shows usage; type `/` to see what your harness offers.",
        ],
      },
    ],
  },
  {
    id: "mcp-tools",
    title: "Give the assistant MCP tools",
    summary: "Register an MCP server once; tick it to send it with every message, or leave it on demand.",
    category: "assistant",
    pages: ["workbench", "settings"],
    keywords: "mcp server tools model context protocol burp stdio http approval probe import json claude cursor vscode",
    steps: [
      {
        title: "Add an MCP server",
        body: [
          "Add MCP server registers a server started from a command (stdio) or reached over Streamable HTTP, or Import brings in the mcp.json you already use with Claude Desktop, Cursor or VS Code. Default approval decides whether its tools pause for your approval. Probe lists the tools it offers.",
        ],
        route: () => "/settings#mcp-settings",
        target: "mcp-settings",
      },
      {
        title: "Send it with every message",
        body: [
          "Tick the server under MCP servers in Assistant settings to send its tools with every message. The assistant loads tools from your other enabled servers only when a message needs them.",
        ],
        route: context => workbench(context, { view: "chat" }),
        action: "open-assistant-settings",
        target: "mcp-turn",
        requiresProject: true,
      },
    ],
  },
  {
    id: "project-knowledge",
    title: "Answer from your project’s documents",
    summary: "Add sources, then let the assistant cite them automatically.",
    category: "assistant",
    pages: ["workbench", "project"],
    keywords: "knowledge sources documents upload url pdf citations library rag",
    steps: [
      {
        title: "Add sources to the project",
        body: [
          "Sources holds the documents the assistant can search: Upload file (text, Markdown, PDF, Word, Excel, CSV, JSON, HTML and more) or Add URL for a public web page.",
          "Items in the global Library are available to every project.",
        ],
        route: context => context.engagementId ? projectSurface(context.engagementId, "sources") : undefined,
        requiresProject: true,
      },
      {
        title: "Check the assistant can use them",
        body: [
          "Knowledge shows how many sources the assistant searches automatically. Answers that use them cite the source.",
          "If it says the profile is text-only, edit the provider in Settings and choose Allow project and document data. Local models always may.",
        ],
        route: context => workbench(context, { view: "chat" }),
        action: "open-assistant-settings",
        target: "knowledge-status",
        requiresProject: true,
      },
    ],
  },
  {
    id: "tool-assistance",
    title: "Get suggested next steps after each command",
    summary: "Tool assistance proposes a reviewed follow-up or drafts a note from results.",
    category: "assistant",
    pages: ["workbench"],
    keywords: "tool assistance suggestion next step notes follow-up sparkles results",
    steps: [
      {
        title: "Turn on tool assistance",
        body: [
          "After a command finishes, Suggest next steps proposes a follow-up action you review before it runs, and Take notes drafts a project note from the result. Both are off until you tick them.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "tool-assistance",
        requiresProject: true,
      },
      {
        title: "Choose the model it uses",
        body: [
          "Tool follow-up needs a model. Pick one under Settings → Automation → Tool follow-up.",
        ],
        route: () => "/settings#post-tool-assistant-settings",
      },
    ],
  },
  {
    id: "missions-and-catch-up",
    title: "Hand work to a mission and catch up later",
    summary: "Continue a harness chat in the background and see what changed when you return.",
    category: "assistant",
    pages: ["workbench"],
    keywords: "mission background continue catch up away unread pending actions missions",
    steps: [
      {
        title: "Continue a harness chat as a mission",
        body: [
          "In a Codex or Grok conversation, open the conversation's actions menu and choose Continue as mission. The work moves to a background mission and keeps going while you do something else.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "continue-mission",
        targetMissing: "Continue as mission sits in the conversation's actions menu, next to its title. Open that menu in a harness conversation; switch the runtime to Harness if the entry is not there.",
        requiresProject: true,
      },
      {
        title: "Follow missions",
        body: [
          "Missions lists background work with its status. Open one to send it guidance while it runs, Discuss in chat, Retry, Stop mission or Delete mission.",
        ],
        route: context => workbench(context, { view: "missions" }),
        requiresProject: true,
      },
      {
        title: "Catch up when you return",
        body: [
          "When a conversation moved on while you were away, Since you last read this conversation summarizes what changed. Review jumps to anything waiting for your decision, such as an approval.",
        ],
      },
    ],
  },
  {
    id: "lifecycle-hooks",
    title: "Run your own script on every chat turn",
    summary: "Lifecycle hooks: create .agents/hooks, then tick the hook for a turn.",
    category: "assistant",
    pages: ["workbench"],
    keywords: "hooks hook.json lifecycle events script automation audit",
    steps: [
      {
        title: "What a lifecycle hook does",
        body: [
          "A hook is a script Nebula runs when a provider chat turn starts, completes, fails or is cancelled. It receives the event as JSON on stdin.",
          "Hooks live in the project folder under `.agents/hooks/<name>/` and only run on turns where you tick them.",
        ],
        callout: "Hooks run on the machine running Nebula Core, not inside the Kali container, with only PATH, LANG and NEBULA_HOOK_* in their environment.",
        requiresProject: true,
      },
      {
        title: "Create the hook files",
        body: [
          "Nebula writes a starter `hook.json` and an executable `run.sh`, then opens them in Code. The starter appends each event to `events.jsonl` beside the script.",
          "Edit either file here; Nebula checks each change.",
        ],
        requiresProject: true,
        starter: { kind: "hook", nameLabel: "Hook folder name", defaultName: "audit" },
        check: hookListed,
      },
      {
        title: "Turn the hook on for your next turn",
        body: [
          "Hooks never run on their own. Tick your hook under Lifecycle hooks. It runs only for turns you send while it is ticked, and the tick clears when you reload the page.",
        ],
        route: context => workbench(context, { view: "chat" }),
        action: "open-assistant-settings",
        target: "lifecycle-hooks",
        targetMissing: "Lifecycle hooks appear in Assistant settings when Runtime is Provider. Open Assistant settings below the message box and choose a provider model.",
        requiresProject: true,
        check: () => onScreen('[data-guide="lifecycle-hooks"] input[type="checkbox"]:checked')
          ? { state: "done", message: "Ticked. Send any message and the outcome appears in the transcript." }
          : { state: "waiting", message: "Waiting for you to tick a hook…" },
      },
      {
        title: "Send a turn and read the outcome",
        body: [
          "Send any message. When the turn finishes, a Lifecycle hooks summary appears in the transcript with each hook’s status and error.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "composer",
        requiresProject: true,
        check: () => onScreen('[data-guide="hook-outcomes"]')
          ? { state: "done", message: "The hook ran. Expand the summary to see its status." }
          : { state: "waiting", message: "Waiting for a turn with the hook ticked…" },
      },
      {
        title: "Make a hook required",
        body: [
          "Set `\"failure_policy\": \"block\"` to fail the turn when the hook fails or times out; `continue` only records the failure.",
          "Set `side_effects` honestly. After a Core restart, only hooks declaring `none` are retried automatically; others wait for you to reconcile them.",
          "Events: `chat.turn.started`, `chat.turn.completed`, `chat.turn.failed`, `chat.turn.cancelled`. Timeout: `timeout_seconds`, 1–300.",
        ],
      },
    ],
  },
  {
    id: "agents-md",
    title: "Give the assistant standing project instructions",
    summary: "AGENTS.md is re-read every turn and never compacted.",
    category: "assistant",
    pages: ["workbench"],
    keywords: "agents.md instructions rules scope system prompt",
    steps: [
      {
        title: "Create AGENTS.md",
        body: [
          "Put the rules that must always hold, such as scope and rules of engagement, in `AGENTS.md` at the project root. Nebula writes a short starter and opens it in Code.",
        ],
        requiresProject: true,
        starter: { kind: "agents_md" },
        check: instructionsFound,
      },
      {
        title: "Share one file across sub-projects",
        body: [
          "A project may link to a parent program’s `AGENTS.md` instead of keeping its own copy. Run this in the project folder on the Nebula host:",
        ],
        command: "ln -s ../AGENTS.md AGENTS.md",
        callout: "The link must point to a file named AGENTS.md in the project folder or one of its parent folders. Links anywhere else are refused.",
      },
      {
        title: "How it is used",
        body: [
          "Provider conversations include `AGENTS.md` in every turn’s instructions, re-read from disk, so edits apply to the next message. Context compaction never summarizes it.",
          "Only the first 64 KiB are sent. Keep it short and put long procedures in skills.",
        ],
      },
    ],
  },
  {
    id: "project-skills",
    title: "Call a project skill with $",
    summary: "Write .agents/skills/<name>/SKILL.md and invoke it from the composer.",
    category: "assistant",
    pages: ["workbench", "settings"],
    keywords: "skills skill.md $ dollar procedure playbook",
    steps: [
      {
        title: "Create a skill",
        body: [
          "A skill is a folder with a `SKILL.md`: a named procedure the assistant follows when you call it. Nebula writes a starter and opens it in Code.",
          "Files the skill links to with relative links, inside its folder, become readable resources.",
        ],
        requiresProject: true,
        starter: { kind: "skill", nameLabel: "Skill folder name", defaultName: "recon-checklist" },
        check: skillListed,
      },
      {
        title: "Call it with $",
        body: [
          "Type `$` in the message box and choose your skill, or type its name after `$`. Arrow keys move through the list; Enter or Tab picks one.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "composer",
        requiresProject: true,
        check: () => /\$[\w-]+/.test((document.getElementById("analyst-message") as HTMLTextAreaElement | null)?.value ?? "")
          ? { state: "done", message: "Skill added to your message. Send it to run the skill." }
          : { state: "waiting", message: "Waiting for a $skill in the message box…" },
      },
      {
        title: "Share skills across projects",
        body: [
          "Skills in the managed folder shown in Settings → Shared skills are available to every project. Copy a skill folder there to share it.",
        ],
        route: () => "/settings#native-skill-settings",
        target: "shared-skills",
      },
    ],
  },
  {
    id: "provider-env-key",
    title: "Use a provider key from an environment variable",
    summary: "Which variable names the desktop app passes to Core.",
    category: "connections",
    pages: ["settings"],
    keywords: "api key environment variable env credential openrouter openai anthropic",
    steps: [
      {
        title: "Name the variable in the provider",
        body: [
          "Add or edit a provider and fill Credential environment variable with a name such as `OPENROUTER_API_KEY`. Nebula Core reads the key from that variable when it calls the provider.",
        ],
        route: () => "/settings#provider-settings",
        target: "add-provider",
      },
      {
        title: "Set it where Core runs",
        body: [
          "The variable must exist in Nebula Core’s environment on the host, not in this browser. For `nebula-core serve` or `nebula-core ui`, export it in the same shell:",
        ],
        command: "export OPENROUTER_API_KEY=…",
        callout: "The desktop app passes Core only known provider variables (OpenAI, Anthropic, Gemini, Azure, Mistral, Cohere, xAI, DeepSeek, Groq, Together, Fireworks, OpenRouter, OrcaRouter, AWS, Google, Hugging Face). Launch the app from a shell where the variable is set.",
      },
    ],
  },
  {
    id: "lan-pairing",
    title: "Pair your phone over LAN",
    summary: "Start Core with --lan, then scan the pairing code in Settings.",
    category: "connections",
    pages: ["settings"],
    keywords: "phone mobile pair device lan qr ios remote",
    steps: [
      {
        title: "Serve Nebula on your network",
        body: [
          "On the Nebula host, start the interface in LAN mode with a TLS certificate your phone trusts:",
        ],
        command: "nebula-core ui --lan --tls-cert cert.pem --tls-key key.pem",
        callout: "--allow-insecure-lan serves plain HTTP to the network. Use it only on a trusted test network.",
      },
      {
        title: "Create a pairing link",
        body: [
          "From the interface opened on the host, choose Pair device and scan the QR code with your phone. Your guide progress follows you to the paired phone.",
        ],
        route: () => "/settings#device-pairing-settings",
        target: "device-pairing",
      },
    ],
  },
  {
    id: "shortcuts",
    title: "Shortcuts and the command palette",
    summary: "⌘K, ⌘, and ⌘1, selection actions, Ask Nebula, composer tricks.",
    category: "keyboard",
    pages: ["workbench", "settings", "project"],
    keywords: "keyboard shortcuts hotkeys palette command ctrl cmd",
    steps: [
      {
        title: "Open the command palette",
        body: [
          "Press ⌘K (Ctrl+K on Windows and Linux) anywhere, or choose Commands. The palette finds pages, settings, project records and guides.",
        ],
        target: "command-palette",
        check: () => onScreen('[data-guide="palette"]')
          ? { state: "done", message: "Palette open. Try typing “vpn” or “mcp”." }
          : { state: "waiting", message: "Waiting for the palette…" },
      },
      {
        title: "Search settings and files",
        body: [
          "Type a setting such as “vpn” or “mcp” to change it in place without leaving the page. Type two or more characters in a project to search its files in Code.",
          "Press → on a result to see its actions.",
        ],
      },
      {
        title: "Jump between pages",
        body: [
          "⌘1 Workbench · ⌘, Settings · ⌥⌘S sidebar · ⌥⌘I activity · ⌘N the page’s main action.",
          "Or press G, then O Workbench, F Findings, R Reports, P Project, L Library, “,” Settings.",
        ],
      },
      {
        title: "Act on selected text",
        body: [
          "Select text in chat, notes or the terminal to Copy, Add to chat, Take note, Run it (terminal) or Ask Nebula. Ask Nebula opens a movable popup; arrow keys move it.",
          "In the terminal, ⌘C / Ctrl+C copies when text is selected and interrupts otherwise.",
        ],
      },
      {
        title: "Message box shortcuts",
        body: [
          "↑ in an empty message box recalls your last message. Paste or drop images to attach them. `$` calls a skill; `/` lists commands for Codex and Grok harnesses.",
        ],
        route: context => workbench(context, { view: "chat" }),
        target: "composer",
        requiresProject: true,
      },
    ],
  },
];

export function guideById(id: string): GuideDefinition | undefined {
  return guideCatalog.find(guide => guide.id === id);
}
