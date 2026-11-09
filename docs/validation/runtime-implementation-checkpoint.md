# Runtime implementation checkpoint

The parallel implementation is on `codex/execution-lifecycle-isolation`. This is
an integration checkpoint, not a release acceptance receipt.

Implemented slices include authenticated host recovery, stable client identity
and request lookup, atomic global SQL APIs, automatic first-project admission,
bounded client request/draft persistence, retained desktop attachment, verified
handoff activation and participant restoration, recovery operation receipts,
code-only deployer rollback, and a volatile desktop browser bridge.

Focused evidence is retained in the sibling validation documents and `/tmp`
receipts. The client slice passed 95 tests and a production build; host bootstrap
and management passed 31 distinct cases; global SQL integration passed 20; global
Settings passed 15 backend and 5 UI cases; browser bridge passed 26. These counts
overlap other selections and must not be summed as a single release result.

## November 9 integration checkpoint

The previously recorded SQLite startup, live-memory control queue and frozen
worker identity defects are fixed. API workers now share their SQL transaction
owner with the global delivery ledger. Positive process evidence permits recovery
from a missing API worker while retaining live or uncertain workers. Global
Storage and guide-progress routes are mounted and keep their explicit global SQL
ownership. Host-wide policy, diagnostics and integration observations are served
without an API worker. These changes have focused receipts in their corresponding
validation documents; they are not a combined release result.

Source-process acceptance passed all five supervisor handoff crash phases on
SQLite and PostgreSQL. Live memory application passed on both databases: a real
bounded allocator's limit changed from 32 MiB to 16 MiB without changing its owner
or resource group, with kernel readback and released capacity on completion.
Participant transfer, paused/disabled work preservation and third-version staging
also have passing isolated process evidence. The five remaining original backend
cases passed in 246.40 s on immutable source `029d9edc`, with unchanged fixtures,
the verified 417-passage product asset and explicitly older `4a39c121` static UI.
This is backend evidence; it does not certify the new UI.

The frozen runtime now uses verified retained onedir workers with exact PID/birth
matching. Independently built baseline `557ffd7d` and candidate `66fdcac0` passed
baseline startup/reconnect (one case, 39.91 s), candidate promotion followed by
retained baseline rollback with continuing SQL data and identity (one case,
54.80 s), and candidate cold-host startup (one case, 39.82 s). Normal shutdown
verified all recorded workers exited. See [frozen runtime evidence](frozen-onedir-runtime.md)
for artifact hashes and limits: these are Core process checks, not installed GUI
close/update/reopen evidence, and do not cover later candidate changes.

Production LAN routing and fixture cleanup are corrected. Desktop Chromium passed
cold-host reserve recovery and lost-response/reload continuity on earlier pinned
source/UI builds. The live-memory UI workflow completed, but its accessibility
check identified low text contrast on Apply; the existing primary button variant
repairs that cause. On `49e59a34`, six Memory Settings component cases, two host
diagnostics cases (SQLite/PostgreSQL), Ruff and the production UI build passed.
The final five-journey Chromium matrix (25 cases at 1440/1024 and 320/390/430)
awaits the combined source/UI pin. It also covers provider tools context and
native standing instructions through inert transports and actual retained
workers. The native fixture does not certify an installed vendor process.

Canonical host integration mutations, session credentials, provider-only durable
tool manifests, and stateful standing-instruction continuity are integrated.
Host Settings and memory-limit application use the canonical host controller;
project workers proxy those operations with exact lease evidence. Production
startup opens the shared Chroma index only after acquiring host ownership and
synchronizes current bundled product guidance. Managed web search is one charged
host service with observed kernel limits; uncontained daemon pull/build work is
refused. Their focused receipts remain separately pinned in the validation docs.

Final integration found and repaired a stale worker capability allowlist; 12
focused checks exercise actual worker startup and schema refusal. Fresh-launch
navigation now restores Core/principal-scoped selected chat identity, with 47
focused UI cases and two production-UI mock browser cases passing. These local
browser checks do not replace the real-Core LAN matrix.

Independent final review confirmed a remaining provider tools renderer defect:
raw global-periodic `BUTLER.md` instructions containing literal managed tools or
hints markers can be removed or rejected on first preparation. JSON-escaped
project instructions, decisions and skills are unaffected. The repair must use
durable explicit fragment ownership and preserve all caller text. See the full
standing-instruction goal/checkpoint; no repair is accepted from a delimiter
shape alone. The final native/browser fixtures also need to use the supported
draft-goal conversation API for an empty newer distractor; generic saved-chat
POST is intentionally unsupported. Both causes were independently validated
before repairs.

The 15 selected mobile WebKit cases cannot execute here: Playwright 1.61.1's
`webkit-2311/pw_run.sh` is absent and no alternate installed engine was found.
The official browser CDN is blocked by this environment's proxy. A focused CI
job must supply genuine WebKit evidence. Native GTK preparation succeeded inside
an owned bounded fixture, but the same official browser resource download block
prevented a genuine native package/GUI run locally; that fixture is stopped.

The native CI job builds two genuine independently pinned packages and runs one
X11/AT-SPI journey with zero retries. Its baseline is `fac1d729` on
`codex/continuity-baseline`: old Core/worker/schema `557ffd7d` plus only the same
fresh-launch navigation feature. Candidate and baseline reopens must observe the
older selected chat and unsent draft before any helper navigation. Only
sanitized evidence is uploaded; private fixture state is excluded. Actual GUI
execution remains pending. See [the native CI recipe](native-desktop-ci-recipe.md).

The current combined selection proposal collects 752 Python and 106 frontend
cases, plus 40 browser cases and one exact Rust test. Final fixture/renderer
repairs will change these counts; the coordinator must review and bind the final
selection before pushing the updated PR. Product guidance is currently verified
at 417 passages with source digest
`4cc8b1f32983bf7231cdc63d5e4af7f862cfbb937124a5dd7f95a78d153c269e`.
PR #960 is still the older draft foundation; later implementation commits are
local until the final integration push. Browser/native gates, final PR review and
CI block merge. Existing installations and live services have not been changed.
Continue from these receipts; do not relabel earlier builds as final acceptance.
