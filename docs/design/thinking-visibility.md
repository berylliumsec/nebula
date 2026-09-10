# Thinking visibility and replay

Journey: open an existing or active conversation, expand activity, expand a thinking
episode, then reload/reconnect. The same provider-exposed text and episode boundaries
must remain discoverable. The transcript stays focused on commentary/final answers.
Core events own content/order; adapters own new episode identity; the reducer repairs
legacy Grok identity using saved message/tool boundaries. React/native details own
only disclosure. No hidden provider content is requested or synthesized.

Required: streaming, completion, interruption, saved/reloaded history, duplicate
replay, long text, keyboard/touch and narrow layouts. Creation/selection use the
existing chat entry; destructive deletion, runtime permissions and project schema
are unchanged. Grok chunks contiguous across metadata/tool updates remain one episode;
a new message or tool start ends it. Completion must drain already queued ACP events.
Codex keeps its provider item IDs and authoritative completed summaries. Thinking
must render for both harnesses without falsely labelling Grok content as Codex.
Display caps must be visibly disclosed; durable fragments remain unchanged.

Verification: selected adapter tests for Grok episode boundaries/completion races and
Codex summary behavior; focused reducer/render tests for both vendors, old replay,
duplicate events and truncation. Committed real-Core saved-event browser journey
using synthetic thinking only, production LAN desktop Chromium and mobile
Chromium/WebKit 320/390/430, accessible expansion and reload. No research commands
or real provider calls are replayed. Physical devices unavailable. Hold Core restart
while work is active; publish static UI independently if needed.

## Stop and Send recovery

The operator stops a turn, sends another message and reloads the conversation.
Every terminal harness turn must retain an assistant activity anchor directly after
its user message, including old turns with events but no final ChatMessage. Core
turns/events remain authoritative; stable recovery IDs are presentation-only and
must not be offered to message bookmark, evidence or fork APIs. Recovery applies
to every authoritative transcript refresh, including while another turn runs.
Intentional cancellation is a neutral Stopped state, not a diagnostics failure.
A cancelled queued dispatch stays cancelled and does not replay automatically;
remaining queued work stays paused. Failures retain their recovery information.
Test recovery ordering, duplicate/reload behavior, active-turn exclusion, and the
production real-Core saved cancelled-turn journey across the existing profiles.

## Queue scroll reachability

An expanded long follow-up (including needs-review) must expose its last action by
scrolling, at desktop and 320–430 px mobile widths. The queue details owns scrolling;
its wrapper and list must not impose shorter nested clipping regions. The composer
retains its existing bounded overflow and touch targets. Core queue data and dispatch
semantics remain unchanged by this layout fix. Add a real-Core production browser
regression that scrolls to and hit-tests the last action, edits and cancels an edit,
and checks the composer remains reachable. No queued request is dispatched.

## Verification evidence (2026-09-09)

- 13 selected backend cases passed: 8 adapter/durable replay cases and 5 queue
  lifecycle/recovery cases. Logs: `/tmp/nebula-thinking-python.log`,
  `/tmp/nebula-stop-python.log`.
- 35 selected component/reducer cases passed (19 activity reducer, 9 ledger,
  5 transcript reconciliation, 2 queue panel). Logs: `/tmp/nebula-thinking-components.log`,
  `/tmp/nebula-stop-components.log`, `/tmp/nebula-queue-components.log`.
- Production build passed: `/tmp/nebula-thinking-build.log`. Ruff and diff whitespace
  checks passed. Reviewed scope receipt: `/tmp/nebula-thinking-selection.json`.
- Committed `thinking.spec.ts`, disposable real Core at LAN
  `http://192.168.1.155:19443`: 24 passed. Desktop Chromium 1440/1024; emulated
  Chromium Android and WebKit iPhone at 320/390/430. Thinking disclosure, saved
  cancelled-turn ordering, retained commentary/partial response, reload, queue last
  action hit testing, editor cancellation, scoped axe and horizontal bounds passed.
  Artifacts: `/tmp/nebula-thinking-queue-final`; log: `/tmp/nebula-thinking-queue-final.log`.
- The queue regression exposed both the final-cascade wrapper limit and a composer
  smaller than its expanded details. Expanded composer capacity now reserves 96 px
  for the transcript while retaining scrollable overflow; closed layout is unchanged.
- Browser fixtures use recorded synthetic events, not real provider work. Adapter
  cancellation/completion is covered below the browser. No research command was
  replayed. Physical devices/software keyboards and a new live-provider Stop and
  Send operation were not exercised. Live existing-turn verification is retained
  separately in the release receipt, so this is not a claim of physical-device or
  new provider execution coverage.
