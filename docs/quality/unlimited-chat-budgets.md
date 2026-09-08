# Unlimited Assistant budgets

Operator journey: send a tool-enabled Assistant message in an existing or new
conversation; work continues beyond five execution calls and twenty artifact
queries until completion, cancellation, or an actionable approval/error.

Authority: Core stores nullable turn budgets and atomic usage counters. The UI
serializes unlimited as null. Provider routing and harness gateway share those
stored budgets. Existing completed turns retain their historical limits.

Invariants: new turns default to unlimited; counters remain accurate without fixed
ceilings; explicit finite API budgets retain enforcement; scope, approval and Stop
remain authoritative. Model context capacity and individual operation timeouts
are not execution budgets.

Lifecycle: create/select/use/stream, approval/resume, interrupt, reconnect/refresh
and retry apply. Fork inherits the new request defaults. Deletion and browser
layout are unchanged and require no new behavior.

Planned evidence: model/request serialization; more than five execution and twenty
query reservations with persistence and idempotency; provider routing regression;
existing approval/cancellation suites; frontend client tests and production build;
real-Core workflow and production LAN checks. Physical Mac verification remains
operator-owned because the operator will install the desktop build themselves.

App-wide audit: RunBudget and mission/automation requests already default token,
cost, duration, tool-call and artifact-query budgets to null. Explicit finite API
budgets remain supported. Autonomous security assessment grants and crawler request
bounds remain unchanged, as do concurrency, transport timeouts, upload limits and
model context capacity. No completed turn is rewritten or replayed. Older desktop
clients still explicitly send twenty artifact queries and need the updated client
for the unlimited query default; Core's execution-call default applies immediately
to their new turns.

Validation so far: 107 storage/chat/queue/API/harness tests passed; 42 frontend API
client tests passed; production build, Ruff and mypy passed. The regression
reserves 210 execution calls and 210 artifact queries per turn, verifies duplicate
reservation idempotency and independent explicit finite-budget enforcement, and
reloads serialized counters beyond the old 205-step ceiling.
