# Packaged desktop initial setup

The sole packaged continuity case failed at candidate `2b4ff750` in CI run
`42685160515`, job `129390041028`. Both independently retained native packages
built and passed their artifact/provenance boundaries. The one case then failed
in 153.25 seconds while its initial AT-SPI setup waited for the exact Project
button after activating Switch project. No native continuity pass is claimed.

## Confirmed preparation contract

The desktop must create Core before the HTTP fixture seeds its Project and one
held chat. The first UI can therefore bootstrap an empty Project catalog. The
old helper sent a synthetic Ctrl+R through X11 and immediately selected controls;
it neither established that this native webview implements that browser shortcut
nor observed a completed fresh UI bootstrap. An asynchronous reload can also
discard a menu opened in the previous document.

Separate fresh-context source review validated this missing preparation
guarantee before repair. The actual failed accessibility tree and document state
were not retained at that early boundary. Wrong catalog state, reload timing and
native shortcut behavior are not individually established as the CI mechanism.
The baseline's exact Project label is rendered as a button, so a role/name repair
is not inferred from the timeout alone.

## Repair

The fixture closes only its initial bootstrap window, through the existing exact
PID/birth-bound WM_DELETE_WINDOW path, after seeding the held turn and before any
chat selection or draft entry. It observes the closed-window state before any
connector or new window could replace a service that incorrectly exited:

- The retained Core PID/birth, baseline manifest/executable and nonsecret
  Core/principal identity remain exact.
- Read-only SQL identifies the same Project owner/fence and execution worker,
  and the same selected API runtime, with positive PID/birth readback. Mutable
  lease expiry and heartbeat timestamps are not compared.
- The original pending turn remains running with one provider execution and no
  release signal or finished worker.

The same baseline desktop then opens normally against that retained state.
Identity and worker observations are repeated before the existing first
select-and-write step. The connection is compared in memory; its secret token is
never added to the receipt. A distinct `bootstrap_attachment` receipt and
bootstrap window phase/log distinguish this preparation from the existing
post-draft baseline close and candidate/rollback reopen journeys.

The synthetic reload implementation is removed. Existing visible Project/chat
selection, real AT-SPI draft input, the once-only setup guard, all subsequent
observe-only reopen paths, held-turn assertions, code rollback, final response
and coordinated shutdown remain. Existing deadlines and the one-producer
boundary are unchanged.

An initial Project-selection timeout now reports bounded structure and state for
known controls and the expected Project, plus a classification of the document
location. It exports no entry values, arbitrary accessible names, hostname,
credentials, query, fragment or private path. Diagnostic failure retains the
original selection error. Serialized diagnostic JSON is capped at 1,000
characters, including escaping, so the driver's existing stderr tail retains it.
Serialization also stays inside the diagnostic failure boundary. The sanitizer's
upload allowlist adds only the exact
`baseline-bootstrap-desktop.log` filename.

## Verification boundary

This repair is based on `48eb7a36`. Product Core, frontend and native application
source are unchanged. Only the dedicated driver, native helper, explicit log
allowlist and this record change. The previously passed fake selection case and
its three-activation/no-Remove assertions are untouched and must not be rerun.

No tests, collection, build, GUI session, service, Mypy or audit ran while
preparing this change. Ruff formatting edited only the two changed fixture
files; it was not a repeated test/check. Final independent static review passed
the lifecycle, normal-exit, identity, later-observation and diagnostic-cap
boundaries. The one still-failed genuine packaged CI journey remains required.
The next shared selection belongs to the coordinator and must exclude every
passing case.
