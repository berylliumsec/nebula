# Host mode and existing conversations

Operator journey: a remotely paired browser changes the project execution policy
from Docker to acknowledged Host mode, then continues the same conversation.
Core policy owns the selected mode; the harness session owns frozen tool specs;
the conversation owns durable history and the current harness binding. The project
folder is on the Core host, never on the browser device.

Observed failure: the warm harness component cache skips the snapshot comparison
already used after Core restart. A Docker tool broker then encounters a new Host
command session and correctly refuses a mismatched execution mode.

At each new chat turn, compare the current command runtime with the frozen snapshot
even when cached. Reuse existing rollover/handoff behavior on mismatch, preserving
chat ID, history and frozen old session. Never change tools mid-turn or automatically
replay a failed command. Explicit Host consent, approvals, timeouts, containment for
workspace-only reads and external MCP locations remain unchanged.

Verification: warm and restarted runtime rollover, retained history/frozen snapshot,
actual Host access to a disposable external folder, Docker path isolation and host
approval/timeout checks. Use focused real-Core browser continuation coverage, with a
fake provider and benign commands only. Verify LAN production, desktop/mobile engines.
Read-only host identity and path access checks establish the filesystem boundary.

## Working indicator and persistence follow-up

Move the existing Working/elapsed summary into the composer as a compact, quiet
line. Preserve plan expansion, pending-action visibility and the active-turn owner;
remove its separate full-width card. Browser verification holds a fake response
open, checks status placement/geometry, reloads mid-turn, then completes it.

Project runtime policy and scope are loaded from Core by project ID, not reset
from UI defaults on reload. Verify all serialized runtime policy values survive
reload and navigation, particularly execution mode, host acknowledgement, approval
policy, networking and timeout. Inspect project scope and metadata authorities too.
Appearance/editor preferences are device-local browser storage, not project policy.

Catch-up follow-up: selecting a summary item navigates to its transcript target and acknowledges the displayed Core read cursor, removing the summary after success. Retain pending actions and show retry on failure. The existing accessible X remains the dismissal control. Verify selection and dismissal with component coverage. Preserve all Working strip information and expandable plans, aligned within composer margins.

## Verification, September 9, 2026

Live records: mode mismatch at 17:17:30 UTC; a new harness session successfully
ran a command at 17:23:14 UTC. No research command was replayed. Core runs as agent
on deep-hacking; the linked project root and named external backup directory pass
OS access checks. Descendant permissions still apply. Live project policy revision
2 records Host mode and acknowledgement; it was not changed by this investigation.

Focused backend: 5 passed (warm/restarted rollover, frozen session/history, actual
benign Host external-folder read, Docker isolation, approval and timeout).
Components: 6 passed across HarnessStatusRail.test.tsx and ChatCatchUp.test.tsx.
Production build and Ruff passed; git diff --check passed.

Committed host-mode.spec.ts: 8 passed against real Core and a held fake provider
at http://192.168.1.155:19441. Desktop Chromium 1440/1024; emulated mobile
Chromium and WebKit 320/390/430. Visible Host save and acknowledgement survive
reload; the entire serialized runtime policy remains equal. Same-chat continuation
rolls over to Host tools, retains history, and survives active/final reloads.
Working retains current step and progress, uses equal composer margins, expands
with a 44px target, and passes scoped axe. Catch-up opens with keyboard/touch,
saves the real Core cursor, disappears and remains absent after reload. Close is
an accessible X. Physical Mac/iPhone/Android testing was unavailable.

Evidence: /tmp/nebula-host-rollover-python.log, /tmp/nebula-host-components.log,
/tmp/nebula-host-rollover-build.log, /tmp/nebula-host-rollover-browser-final.log,
and /tmp/nebula-host-rollover-browser-final. Scope and metadata persistence were
source-audited; this is not exhaustive browser testing of every setting.

Release constraint: do not restart Core during an active research turn. The static
UI may be published with hashed assets retained and index replacement last. Core
activation and restart verification remain pending until the service is idle.

## Collapsible command failures

The operator can expand/collapse a failed command directly from the attention row
without opening the entire activity audit. Keep its name, failed status and brief
error in a compact collapsed summary, with full details/actions inside. Pending
approvals retain their existing visibility. Disclosure is presentation state; saved
Core events remain authoritative and reload starts collapsed. Verify component
expansion across live rerenders and saved real-Core failures with keyboard/touch,
44px targets, axe, reload and production LAN desktop/mobile Chromium/WebKit.

Failure disclosure evidence: ActivityLedger.test.tsx 8 passed; committed
workspace-recovery.spec.ts 8 passed in the permanent 1440/1024 desktop Chromium
and 320/390/430 mobile Chromium/WebKit profiles. Real Core serves saved synthetic
failed Grok events; no provider or command executes. Production LAN origin
http://192.168.1.155:19442. Keyboard/touch toggle, 44px summary, scoped axe,
visible failure identity, collapsed reload, live-rerender expansion preservation,
and no horizontal clipping passed. Production build passed. Physical devices
remain unavailable. Artifacts: /tmp/nebula-failure-collapse-browser-final; logs:
/tmp/nebula-failure-collapse-components.log and /tmp/nebula-failure-collapse-build.log.
