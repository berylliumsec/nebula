# Browser interception resume recovery

## Operator journey

A macOS operator uses the native Workbench browser while connected to a remote
Core, enables interception, starts a navigation, and then chooses **Resume
requests**. The current page must continue even if the native pause event never
reached the durable Intercept queue.

## Observable invariants and state authority

- Core is authoritative for the durable interception setting and research
  records.
- The native session proxy is authoritative for live request and response
  waiters that have not completed.
- Disabling interception releases every waiter already owned by that proxy and
  allows new traffic to pass without pausing.
- Explicit Forward and Drop remain single-use decisions while interception is
  enabled.
- A missing UI listener or durable receipt may fail closed while interception
  remains enabled, but it must not make **Resume requests** ineffective.

## Lifecycle and test layers

The selected lifecycle covers enable, request/response pause, lost event,
resume, subsequent navigation, re-enable, explicit decisions, tab close, and
application reconnect. Focused Rust tests prove pending waiter release and
single-use decisions; component tests prove the operator-facing recovery
contract. The packaged macOS WebDriver journey remains the real native gate.

