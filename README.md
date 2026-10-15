<div align="center">
  <img src="images/logo.png" alt="Nebula Quiet Lens logo" width="120" height="120">
  <h1>Nebula</h1>
  <p>Cybersecurity harness built around the operator</p>
</div>

![Quiet Lens in an empty Nebula chat, revealing the separate navigation, Session rail, composer, and top menu on hover](docs/images/quiet-lens-blank-tour.gif)

## Acknowledgement

First, I would like to thank Almighty God, who is the source of all knowledge. Without Him, this would not be possible.

> Use Nebula only on systems and networks you own or are explicitly authorized to test.

## Key features

- **One Project for the engagement.** Keep assets, browser sessions, Kali terminals, code, files, notes, findings, and reports together.
- **Security browser and interception.** Capture in-scope traffic in the desktop browser, intercept and edit requests, and use Repeater and Intruder for follow-up testing.
- **Bring your own skills.** Add `SKILL.md` skills to a Project and select them in chat for assistant work.
- **Kali tools and optional VPN egress.** Run container commands with Docker or Podman and route authorized container traffic through a Project-selected OpenVPN profile.
- **Operator-controlled automation.** Set scope, approvals, and budgets for assistant and mission actions.
- **Evidence through reporting.** Preserve hashed artifacts and execution provenance, link evidence to findings, and export reports or engagement bundles.

## Butler

<img src="ui/public/nebula-logo.svg" alt="Nebula logo used to open Butler" width="23" height="36">

Select the Nebula logo in the header or mobile navigation to open Butler, then use chat or Voice. With a configured model or harness, Butler can answer questions, review the selected project's chats and goals, start or steer work, and manage settings and approvals. It shows delegated work separately from main agents; to redirect a child, ask Butler to guide its parent agent.

<picture>
  <source media="(prefers-reduced-motion: reduce)" srcset="docs/images/butler-voice-sample.png">
  <img src="docs/images/butler-voice-sample.gif" width="100%" alt="Butler Voice in an illustrative Northstar API Review project: the listening orb moves as an operator asks it to start an authentication review, then Butler replies and the new assistant appears in Project work.">
</picture>

<p><sub>Illustrative sample project. This GIF is silent; Butler speaks in an active Voice session.</sub></p>

When a goal starts, Butler watches its supported progress events even if the panel is closed. The project can grant Watch, Steer, or specific Delegate actions. GPT-Live listens and speaks in Voice mode; the Core Butler runs the tools, owns the saved conversation and context compaction, and continues oversight while Voice is off. Voice does not yet speak unsolicited alerts. Separately, Butler checks relevant precedent before acting and keeps durable, selective memory of useful preferences, decisions, and lessons across projects. Open Memory in the Butler panel whenever you want to review, edit, or delete an entry.

A fresh Core data directory starts with empty Butler memory. Its SQLite records stay in that data directory across app updates, and the Chroma search index can be rebuilt from them.

<p align="center">
  <img src="docs/images/nebula-butler-architecture.svg" width="620" alt="Nebula architecture: a host runs Nebula, with optional independent Mercury controls. Butler directs main agents, which coordinate subagents. Operator-provided skills, hooks, and software extend agent work.">
</p>

## Work

Use Codex or Grok as harnesses, or connect Claude, OpenRouter, Ollama, or vLLM. Docker or Podman powers Kali and container-mode automation; Host mode uses the host's filesystem and network after explicit acknowledgement. Optional OpenVPN routes container traffic, while [Mercury](https://github.com/berylliumsec/mercury) can add independent controls when enrolled.

## Install

The Linux x86_64 preview is available through the [signed APT repository](https://berylliumsec.github.io/nebula-apt/) or as a [DEB or AppImage](https://github.com/BerylliumSec/nebula/releases). Back up engagement data before upgrading.

## From source

Install Python 3.11–3.13, Poetry 2.1.3, Node.js 20 with npm, Rust, and the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/), then run:

```console
git clone https://github.com/BerylliumSec/nebula.git
cd nebula
poetry install --with dev
poetry run playwright install chromium
npm --prefix ui ci
npm --prefix ui run dev:desktop
```

## More

[Nebula 3 guide](docs/NEBULA3.md) · [Usage scenarios](docs/NEBULA3_USAGE_SCENARIOS.md) · [Automation runtime](docs/AUTOMATION-RUNTIME.md) · [Diagnostics](docs/NEBULA3_DIAGNOSTICS.md) · [Release notes](docs/releases/)
