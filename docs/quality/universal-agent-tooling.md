# Shared Core tools and mixed agent runtimes

Entry: a project's Assistant settings independently selects a provider or harness
for its main conversation and children, and selects project lifecycle hooks.
Core owns selections, lineage, limits, immutable hook snapshots, receipts and
final answers. Vendor sessions own transport and private context. Browser state
owns only transient selection presentation; saved configuration belongs to Core.

| Journey | Invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover | Parent launch availability and child further delegation are distinct; hooks and Core tools are discoverable for either backend | Core catalogs | Focused Core tests |
| Select/save | All four parent/child combinations and hooks survive reload | Core Assistant config | Component and real-Core browser |
| Start/use | Immutable required start hooks pass before task work | Core hook runner | Core integration |
| Report/complete | Shared handlers retain policy and ownership; completion guards pass before publishing answers | Core ledger and goal | Core integration |
| Stream/stop | Stop cancels gateway work and preserves actual hook outcomes | Core turn ownership | Focused lifecycle regressions |
| Reconnect/retry | Durable choices and lineage return without duplicate mutations or wider child access | Core database | Core and browser |
| Revoke/delete | Disabled runtimes, changed hooks, stale revisions and depth limits fail closed | Core admission | Core integration |

Validation uses disposable synthetic Core data and inert runtimes. Physical
device and live paid-provider evidence are separate from fixture acceptance.

Independent pre-edit diagnosis confirmed backend-dependent child selection,
rejected harness hook assignments, ambiguous capability responses, unconsumable
capability cursors and separately assembled tool catalogs. Automatic command
`tool.before`/`tool.after` hooks already apply to both backends and remain
distinct from selected turn lifecycle hooks and vendor-native hooks.

## Candidate evidence (2026-11-04)

Base: `5d93935ffcb5b084a62354a71f0e533d57672734`. Branch:
`codex/universal-agent-tooling`. `.github/test-selection.json` selects 111 Python
cases (109 Core regressions and two product-help gates), 17 frontend cases and
one browser journey across eight permanent profiles. No full suite, paid vendor,
production Core, source job or simulation was used.

- **108 Core cases passed** using the exact non-product-help Python node IDs in
  the selection. They include 29 initially new cases for all four runtime combinations,
  guard rejection, hook receipts/cleanup, pagination, notes/research/catalog
  tools, assignments, skill snapshots/resources, retry confinement, native tool
  clamping, parent/child questions, goal ownership/usage and remote-data consent.
- **17 frontend cases passed** in `chatSubagentChoice.test.ts`,
  `HarnessSubagentSettings.test.tsx` and `chatSubagentsSettings.test.ts`.
- `ruff check`, `ruff format --check`, Python/Rust and frontend diagnostic
  audits passed. `mypy src/nebula/v3` passed for 236 source files.
- `npm --prefix ui run build` passed. The served production tree SHA-256 was
  `601242f095ded10a0f9ab1c7c01a608070ac114b5e91232eb673ab40b28cd64f`.
- The committed `universal_agent_fixture.py` serves authenticated, isolated Core
  data and that production bundle. Normal device pairing, settings changes,
  reload and all four child runtime combinations were exercised through the UI.
  The browser reads the resulting child backend/status and actual start and
  completion hook executions from Core. Dialog axe checks and horizontal
  clipping assertions passed.
- **Five browser profiles passed** on non-loopback LAN origins
  `http://172.26.91.241:22686` (1440 desktop) and
  `http://172.26.91.241:22687` (1024 desktop; Pixel emulation at 320/390/430).
  Commands selected only `tests/universal-agents.spec.ts` and those projects,
  using `NEBULA_TEST_PYTHON`, `NEBULA_MODEL_TEST_HOST/PORT` and the installed
  Chromium executable. The disposable fixtures reported degraded diagnostic
  availability; they used inert local provider/harness transports, not live
  vendor services. Physical devices were unavailable.

Eight old test failures reproduced independently on untouched base main. They
are listed exactly in the selection's `baseline_failures`: consent/effort,
provider-parent messaging and goal/scheduled-choice fixtures. Their assertions
were not weakened. The selected existing passing cases cover the relevant
lifecycle and recovery behavior.

## Acceptance still required

Local WebKit installation was denied by network policy (403 from
`cdn.playwright.dev` and `playwright.download.prss.microsoft.com`). The three
committed iPhone/WebKit widths 320/390/430 remain selected for normal PR CI.
No local WebKit pass is claimed. Initial commit `e38a0f3b` subsequently passed
all eight browser profiles in normal PR CI, including all three WebKit widths
([browser run](https://github.com/berylliumsec/nebula/actions/runs/41828680366)).
The same commit passed 110 selected Python checks, including both product-help
checks in CI's regenerated checkout, and the 17 frontend checks
([CI run](https://github.com/berylliumsec/nebula/actions/runs/41828680382)).
A follow-up adds the existing operator reconciliation API and recorded late
outcome recovery for fenced harness hooks; it releases the interrupted chat only
after all unknown effects are resolved. This adds one parameterized regression
case (30 new cases total). All 30 new cases passed locally after the follow-up,
and six targeted provider/harness cleanup cases also passed. Final-head CI is
reported on the PR.

Product-help regeneration and both selected index/digest-to-Chroma checks were
attempted. The embedding model download at
`chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz` was denied by
network policy (403). The bundled product index is therefore still stale;
its digest and embeddings were not fabricated. Regenerate the committed index
with `python scripts/build_product_knowledge.py`, then verify the two selected
`test_product_knowledge.py` cases in an environment with the approved model.
The normal CI tests may regenerate their own checkout, which does not replace
committing the candidate's updated index.

The PR must remain unmerged until the product-help artifact, Chroma synchronization
and required final-head evidence are available, per
`.codex/rules/change-workflow.md` and the product-quality skill. Temporary Core
instances and synthetic data are removed after evidence collection.

Notebook MCP needs explicit selection on **both** backends; storage deliberately
excludes it from automatic on-demand discovery. Other usable MCP catalog sources
now use the shared discovery/load/call path. Butler administration stays scoped
to the settings assistant.
