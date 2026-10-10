# Nebula agent guidance

Read and follow these repository rules before working in Nebula:

- [Branch, validation, and PR workflow](.codex/rules/change-workflow.md) for every change.
- [Focused test selection](.codex/rules/test-selection.md) before running tests or opening a PR. The detailed procedure is in [docs/TEST_SELECTION.md](docs/TEST_SELECTION.md).
- [Operator workflow rules](.codex/rules/operator-workflows.md) when changing a feature an operator uses.

For any operator-visible interface, mobile, LAN, chat/session lifecycle, provider,
harness, workspace, or API-to-UI change, read and follow the
[nebula-product-quality skill](.agents/skills/nebula-product-quality/SKILL.md)
**before editing**, including for fixes and reviews. Apply the
[interface principles](docs/design/interface-principles.md) to touched controls.
The skill and its quality-gates reference define the acceptance evidence. If a
required gate is unavailable, report the work as incomplete or partially verified
and name the missing evidence. Never claim an untested workflow is complete.
