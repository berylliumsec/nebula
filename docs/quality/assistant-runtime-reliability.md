# Assistant runtime reliability

Journey: Settings → configure/check Codex or Grok → Assistant → send, change settings,
continue, interrupt, reload and retry. Core owns profiles, chat identity, transcript
and runtime snapshots. React owns unsent settings and presentation; each running
vendor session retains its frozen permissions and options.

Invariants and planned layers:
- Catalog discovery: an unavailable MCP catalog cannot hide usable harnesses;
  independent loading, visible error and retry (component + production browser).
- Connect/use: bounded startup, cancellation releases subprocess and gateway;
  large valid protocol frames retain the connection (adapter regressions).
- Health: Codex authentication is checked, including providers not requiring
  OpenAI authentication (adapter regressions).
- Settings: subsequent turns use selected options while retaining the chat and
  history. Changed harness settings create a new frozen runtime session with a
  context handoff; active work is not mutated (Core + browser).
- Refresh/reconnect: saved chat settings and transcript return from Core (real
  Core production browser). Failure/retry stays in the current screen.
- Create/select/use/stream/interrupt/refresh/retry are applicable. Deletion,
  credential revocation and physical keyboard behavior are unchanged.

Focused selection will be collected and bound to the diff before execution.
No full suites. Physical devices and live vendor credentials are not assumed
available; fixture evidence will be identified explicitly.

Implemented behavior:
- Codex stdio accepts bounded large frames; both adapters bound startup to 60s
  and close their transport on timeout/cancellation. Gateway cancellation cleans up.
- Codex health refreshes/reads account state and explains host-side login recovery.
- Catalogs load independently, including while the other catalog is pending.
- Runtime/provider/model/effort/speed/MCP controls remain available between turns.
  Core preserves the chat/transcript and hands context to a new frozen harness
  session when options change. Existing vendor sessions keep their original options.
- Assistant failures stay behind Show activity with a compact warning count;
  actionable attention entries remain visible. Retrieval schemas reject malformed
  arguments with the required/accepted fields and retry guidance.

Validation (September 11, 2026):
- 61 selected Python cases pass (59 initial passes; the two new provider fixture
  cases corrected and passed; startup and normalized-default cases rerun).
- 14 component tests pass in HarnessSettings.test.tsx and ActivityLedger.test.tsx.
- Production `npm --prefix ui run build`, focused mypy, Ruff and diagnostic audits pass.
- Real Core + inert adapter, production static bundle, non-loopback LAN IPv4:
  assistant-real-desktop (1440), assistant-real-compact (1024), emulated Android
  Chromium (320/390/430), emulated iPhone WebKit (320/390/430) pass. The 320px
  WebKit response initially exceeded the 5s assertion; it passed after using a
  bounded 20s response wait. Other seven profiles passed in the first matrix run.
- Journey: create/send → optional activity expansion → change model/effort →
  continue in same chat → reload/restored settings/history → independent catalog
  failure → retry. Logs: /tmp/assistant-matrix.log, /tmp/assistant-webkit-retry.log,
  /tmp/assistant-python.log, /tmp/assistant-python-retry.log,
  /tmp/assistant-settings-retry.log, /tmp/assistant-components.log,
  /tmp/assistant-build.log. CI repeats the committed selection.
- Limits: no physical-device or real-vendor credential acceptance; protocol
  failures use inert fixtures. No new full accessibility/keyboard/landscape audit
  or deployed-service upgrade was run. This is focused workflow verification,
  not an unqualified claim of every product-quality gate or live vendor readiness.
