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

## Nebula assistant

<img src="docs/images/nebula-assistant-icon.svg" alt="Nebula assistant bot icon" width="36" height="36">

Find the bot icon in Nebula's upper-right corner. It helps you find and open settings or pages; with a connected model or harness, you can ask questions about Nebula and troubleshoot setup or workflow issues.

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
