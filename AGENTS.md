# Nebula Agent Guidance

## Mandatory product-quality skill

For every operator-visible interface, mobile, LAN, chat/session lifecycle,
provider, harness, workspace, or API-to-UI change, agents **must** read and follow
`.agents/skills/nebula-product-quality/SKILL.md` before editing. This requirement
also applies to bug fixes, reviews, and claims that a feature is complete.

Before implementation, record the operator journey, observable invariants, state
authorities, lifecycle coverage, and planned test layers. Before claiming success,
exercise the real workflow and report the skill's completion evidence. A mocked
test, development build, resized desktop viewport, or one-off browser check cannot
stand in for a required real-Core, production, mobile-browser, LAN-origin, or
physical-device gate.

If a required gate is unavailable, report the work as incomplete or partially
verified and name the missing evidence. Never describe an untested workflow as
supported, fixed, production-ready, or complete.

## Product standard

Nebula is an operator product, not a collection of API endpoints. Implement the
complete user workflow and judge success from the operator's screen. A backend
record that cannot be discovered, selected, understood, or used in the UI is not
finished.

## Operator-quality principles

1. **Complete the last mile.** Whenever a workflow asks for a value users would
   normally browse, discover, preview, or select, provide that interaction. Do
   not make users type opaque IDs, absolute paths, model names, or capability
   strings when Nebula can enumerate them safely.

2. **Design the happy path to require no product knowledge.** New capabilities
   must appear in every relevant catalog, type union, selector, default-runtime
   decision, status view, and empty state. Healthy configured runtimes should be
   selected automatically for new work, while remaining easy to change.

3. **Use progressive disclosure.** Keep primary content and decisions prominent.
   Collapse verbose reasoning, tool traces, protocol events, diagnostics, and
   historical activity into compact summaries by default. Never collapse an
   approval, user-input request, failure, or other item requiring operator action.

4. **Protect operator attention.** Avoid noisy feeds, raw JSON, repeated notices,
   layout jumps, and controls that do not apply. Summarize activity with status,
   progress, counts, and the next required action; retain details behind an
   explicit expansion control.

5. **Respect where the UI is running.** Distinguish the browser device from the
   Nebula host. A phone file picker cannot select a server folder. Build a bounded
   server-side browser or a clear handoff when an action belongs to the host.
   Test production HTTP/HTTPS origins, because localhost-only browser APIs may be
   unavailable on LAN addresses.

6. **Make state changes visibly authoritative.** A success message must be
   followed by the saved item appearing in its list and becoming selectable in
   downstream workflows. Reload from the durable source after mutations and test
   against stale filters, caches, reconnects, and duplicate submissions.

7. **Choose coherent defaults.** Infer defaults from the selected provider,
   harness, project, and advertised capabilities. Do not require unrelated setup
   for a basic operation: optional command runtimes, tools, images, or knowledge
   features must degrade independently rather than blocking text chat.

8. **Treat a project folder as one workspace.** When an operator links a folder,
   Files, Code, terminals, containers, harness `cwd`, instruction discovery, and
   skills must resolve to that same folder. Do not copy it into a hidden parallel
   workspace. Freeze permissions per session and prevent bulk-reset operations
   from deleting linked host folders.

9. **Keep security explicit and usable.** Dangerous modes require unmistakable
   opt-in and warnings, but authorized workflows should then work without hidden
   authentication or capability gates. Never weaken unrelated boundaries as a
   convenience fix.

10. **Explain recovery in place.** Errors must state what failed, what still
    works, and the next valid action. If an automatic retry is safe, offer it. Do
    not send operators to logs or Settings when the current screen can resolve the
    problem.

## Definition of done for interface changes

Before calling a feature complete:

- Exercise the workflow from its real entry point through the visible result.
- Verify create, refresh, selection, use, failure, retry, and reconnect behavior.
- Test the production bundle, not only development mode or direct API calls.
- Test at least one desktop viewport and one 320–430 px mobile viewport.
- Test on a non-loopback LAN origin when browser security behavior can differ.
- Confirm loading, empty, disabled, error, success, and long-content states.
- Confirm keyboard, touch, screen-reader labels, focus, and reduced-motion behavior.
- Confirm supported capabilities are visible and unsupported ones are hidden or
  explained without vendor-specific UI forks.
- Confirm the live or packaged build actually contains the latest change.
- Run the browser, state, interaction, origin, and build matrices required by the
  `nebula-product-quality` skill and retain exact evidence for the handoff.

When a user reports a missing quality-of-life behavior, fix the immediate issue
and identify the broader product rule that would have prevented the omission.
