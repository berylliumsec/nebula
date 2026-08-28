# Security Browser operator contract

This contract is the implementation and release boundary for the first-class
Security Browser. It describes what an operator must be able to accomplish and
observe before any individual component or engine can be described as complete.

## Journey and entry point

The real entry point is **Project -> Workbench -> Security Browser**. The primary
journey is:

1. discover the Security Browser and understand whether desktop execution is ready;
2. create a guided assessment from an enumerated in-scope target, browser identity,
   objective, and scan profile;
3. review the frozen scope revision, risk classes, grants, and request budgets;
4. start the assessment and observe ordered progress, traffic, coverage, and
   candidate issues;
5. take over for login, MFA, CAPTCHA, consent, or an ambiguous browser state, then
   return control without changing identity;
6. pause, resume, stop, retry, refresh, reconnect, and background the interface
   without losing or duplicating acknowledged work;
7. validate a candidate through a separate bounded grant and promote it only after
   operator evidence review;
8. revoke or delete the assessment and clear stale URL selection.

Forking is not part of the first assessment lifecycle. A new objective creates a
new assessment so scope, identity, evidence, and budgets remain attributable.

## State authorities

| State | Authority |
| --- | --- |
| Assessment, steps, coverage, traffic metadata, candidates, evidence | Core database |
| Selected assessment, tool, and item | URL query parameters |
| Scope, grants, and budgets | Frozen Core policy revision |
| Live browser process, profile, raw secret-bearing state | Desktop browser engine |
| Passwords, cookies, tokens, reusable credentials | Browser profile or OS vault |
| Device-local layout preferences | Browser storage |
| Open panels, drafts, selection gestures | React component state |
| Connection and replay cursor | Core event stream plus durable snapshot fallback |

## Observable invariants

- Manual and autonomous work share one selected identity, traffic history, scope
  revision, and evidence chain.
- A successful mutation is followed by the authoritative object appearing in its
  list and becoming selectable without a reload.
- Reload, reconnect, backgrounding, browser-worker restart, and receipt retry do
  not repeat a non-idempotent action.
- No request leaves the frozen scope or exceeds the assessment or validation
  grant budget.
- Secrets never enter model-visible arguments, Core receipts, diagnostics, or
  assessment events.
- Approvals, user-input requests, failures, expiring grants, and emergency stop
  remain visible and actionable; verbose traces stay collapsed by default.
- Every failure names what failed, what remains usable, and the next valid action.
- Mobile and LAN clients may monitor and stop, but never claim native commands or
  silently imply that a desktop-only action executed.
- Candidate issues are not confirmed Findings until validation and operator review.

## Acceptance layers

| Journey step | Required evidence |
| --- | --- |
| Discover and create | Component plus desktop/mobile Playwright |
| Save and select | Real Core plus URL deep-link/reload |
| Execute and stream | Managed browser adapter plus real Core event replay |
| Take over and interrupt | Packaged desktop workflow |
| Refresh and reconnect | Real Core plus browser-engine restart injection |
| Failure and retry | Component, real Core, and adapter fault injection |
| Stop and revoke | Real Core plus native egress/command cancellation |
| Delete | Real Core plus stale-URL recovery |

Release evidence must use the matrix in
`.agents/skills/nebula-product-quality/references/quality-gates.md`. A mocked API,
development bundle, resized desktop viewport, or source inspection is never a
substitute for a required real-Core, packaged desktop, LAN, mobile-browser, or
physical-device gate.
