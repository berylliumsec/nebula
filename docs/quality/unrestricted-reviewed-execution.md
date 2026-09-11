# Unrestricted reviewed execution acceptance

## Contract

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover | Runnable Bash, sh, and Python blocks expose Review and run when a network-capable runtime is available. | Core execution capabilities | Component + real Core |
| Review | The dialog shows exact source and unrestricted outbound access; it asks for no offline/scoped choice, target, or ports. | React request derived from Core capability | Component + Playwright |
| Start | Preflight and start are bound to identical source, runtime, network mode, limits, operator, and preview fingerprint. | Signed Core preflight and durable execution | Python + real Core |
| Execute | A fresh non-root, read-only-root container uses bridge networking with no target pinning or scoped egress rules. | Core sandbox request | Python |
| Retry | Existing preview expiry, idempotency, cancellation, and failure recovery remain authoritative. | Core execution record | Existing execution suite |
| Terminal prompt | A command result without a newline cannot collide with the next prompt. | Bash prompt hook inside terminal container | Shell integration test |

The reviewed source remains the mandatory decision boundary. This change does
not bypass source review, run commands on the host, add interactive stdin to a
reviewed execution, or alter project approval-policy prohibitions.

## Lifecycle and state

- Entry: editor task or runnable assistant code block.
- State owners: Core owns capabilities, signed preflight, execution, and output;
  React owns only the open dialog; the container PTY owns cursor presentation.
- Refresh/reconnect, duplicate start, cancellation, output persistence, and
  terminal recovery continue through their existing durable paths.
- Deletion is not applicable: executions are retained audit records.

## Verification record

Populate final automated and browser evidence in the PR after the release gate.
Mobile Chromium exposed a separate compact Code-action handoff defect before the
review dialog opens; mobile review acceptance is therefore unverified here.
