# Minimal agent instructions

## Operator journey

An operator selects a provider or harness and starts work from Nebula. Nebula passes
the operator request, workspace context, available tools, and required response
shape without adding a competing persona or behavioral policy. Instructions from
the selected provider and the linked project, including `AGENTS.md` and skills,
remain effective.

## State authorities

- The operator request owns the task.
- The linked project owns project instructions and skills.
- Provider and harness sessions own their native instruction state.
- Nebula Core owns workspace routing, available capability metadata, tool schemas,
  structured response schemas, and conversation persistence.

## Observable invariants

- Nebula does not add trust classifications, extraction policy, writing policy,
  or general assistant behavior to model prompts.
- Harness prompts identify Nebula and map project operations to the supplied tools
  with project root `.`.
- Dynamic capabilities and exact tool or response schemas remain available where
  parsing and routing depend on them.
- Browser and application-model tools remain discoverable when available.
- Stored context, references, screenshots, and execution artifacts use neutral
  labels.
- Reconnect, resume, and provider selection reuse the same minimal prompt contract.

## Lifecycle and verification

Provider discovery, selection, turn creation, tool use, streaming, interruption,
resume, retry, and reconnect are applicable. UI layout, mobile geometry, LAN-origin
browser behavior, deletion, and revocation are not changed by this contract.

Focused unit coverage checks the exact model-facing strings and provider adapter
wiring. Existing component coverage checks neutral browser attachment labels. A
production UI build verifies the changed TypeScript paths. Live provider and
harness calls remain a release gate because unit adapters cannot prove vendor-side
instruction precedence.
