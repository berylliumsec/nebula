# Nebula agent guidance

Read and follow these repository rules before working in Nebula:

- [Branch, validation, and PR workflow](.codex/rules/change-workflow.md) for every change.
- [Focused test selection](.codex/rules/test-selection.md) before running tests or opening a PR. The detailed procedure is in [docs/TEST_SELECTION.md](docs/TEST_SELECTION.md).
- [Operator workflow rules](.codex/rules/operator-workflows.md) when changing a feature an operator uses.
- [Code troubleshooting](.agents/skills/code-troubleshooting/SKILL.md) for defects, regressions, and failing behavior. Identify the root cause and have a separate subagent validate it before attempting a fix.

For any operator-visible interface, mobile, LAN, chat/session lifecycle, provider,
harness, workspace, or API-to-UI change, read and follow the
[nebula-product-quality skill](.agents/skills/nebula-product-quality/SKILL.md)
**before editing**, including for fixes and reviews. Apply the
[interface principles](docs/design/interface-principles.md) to touched controls.
The skill and its quality-gates reference define the acceptance evidence. If a
required gate is unavailable, report the work as incomplete or partially verified
and name the missing evidence. Never claim an untested workflow is complete.

When adding an operator feature or changing a setting, update the dedicated
Nebula settings assistant's destinations, action discovery, and product guidance
in the same change. Update `ui/src/settingsCatalog.ts` and
`docs/design/nebula-settings-assistant.md` where applicable, regenerate and verify
the bundled product knowledge index, and verify that Core synchronizes its current
source digest into the Chroma product-help collection. Add a focused regression
check so the assistant can discover and use the feature or setting. Do not call a
browser action saved until Core or the device reports the observed result.
