---
name: nebula-product-quality
description: Enforce end-to-end product quality for Nebula interface, mobile, LAN, chat/session lifecycle, provider, harness, workspace, and other operator-visible changes. Use whenever implementing, fixing, reviewing, or claiming completion of a workflow that an operator reaches through the Nebula UI, especially changes under ui/, API-to-UI integrations, persistence/reconnect behavior, responsive layouts, or browser security boundaries.
---

# Nebula Product Quality

Treat each change as an operator workflow, not an isolated component. Establish the
behavioral contract before editing, test it through the real entry point, and do not
claim completion without evidence for every applicable gate.

## 1. Establish the contract

Before implementation:

1. Name the operator journey and its real entry point.
2. List the state owners involved: URL, browser storage, React state, Core database,
   provider or harness session, workspace, and connection state.
3. Write observable invariants. Include persistence and responsive invariants when
   relevant, such as “the selected chat and transcript survive reload” or “the
   focused composer remains inside the visual viewport.”
4. Enumerate the lifecycle: discover, create, select, use, stream, interrupt,
   background, reconnect, refresh, fork or retry, delete or revoke.
5. Mark each lifecycle step applicable, not applicable with a reason, or required.

Do not begin with CSS selectors or endpoint shapes. Begin with what the operator
must be able to accomplish and observe.

## 2. Inspect the complete path

Trace every required lifecycle step across UI, API client, Core route, durable
storage, and provider or harness adapter. Identify duplicate state ownership and
resolve authority explicitly. Prefer:

- durable server state for transcripts, lineage, activity, and resumable work;
- the URL for shareable or reloadable navigation identity;
- browser storage only for device-local preferences and a documented fallback;
- component state only for transient presentation and unsent input.

Avoid fixing a state-ownership defect with an unrelated CSS or timing workaround.
Avoid adding vendor-specific UI when a normalized component can represent the
capability.

## 3. Build tests before claiming the fix

Add regression coverage at the layer where the defect escaped:

- Unit or component tests for deterministic transformations and local states.
- Mocked Playwright tests for loading, empty, failure, long-content, focus, touch,
  accessibility, and exact responsive geometry.
- Real-Core Playwright tests for persistence, mutations, streaming, reconnect,
  refresh, authorization mode, and workspace behavior.
- A production-bundle LAN check when origin, cookies, WebSockets, PWA behavior,
  filesystem location, or mobile browser behavior is involved.

Do not accept a mocked API test as proof of persistence or reconnection. Do not
accept a one-off browser script as a substitute for a committed regression test.

Read [quality-gates.md](references/quality-gates.md) for the required matrices and
evidence format.

## 4. Dogfood the operator journey

Use the running product through the same visible controls the operator uses. For a
session feature, exercise one continuous session through all applicable lifecycle
steps. For a mobile feature, focus the real input, open the software keyboard when
physical-device access exists, rotate when relevant, background and resume, and
verify that content remains reachable without horizontal clipping.

Record the origin, build type, browser engine/profile, viewport, and workflow
result. Clearly distinguish physical-device testing, device emulation, and a
resized desktop viewport.

## 5. Run the release gates

Run every applicable gate in the reference matrix. Ensure mobile browser projects
are permanent entries in Playwright configuration rather than ad hoc invocations.
At minimum, interface work requires:

1. relevant unit/component tests;
2. relevant Playwright tests in desktop Chromium and mobile Chromium;
3. relevant Playwright tests in mobile WebKit;
4. a production build;
5. a real-Core workflow for any server-owned or resumable state;
6. a non-loopback LAN-origin check for origin-sensitive behavior.

If a gate cannot run, state that the change is incomplete or partially verified.
Never convert “not tested” into “supported,” “fixed,” or “complete.”

## 6. Report evidence

Lead the handoff with the operator-visible outcome. Then report:

- workflow exercised;
- automated commands and pass/fail counts;
- browser engines, device profiles, and whether each was emulated or physical;
- live origin and production/development build type;
- any skipped gate and the resulting limitation.

Do not use a generic “tests passed” statement when the required product journey
was not exercised.
