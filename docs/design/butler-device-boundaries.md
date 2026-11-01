# Butler device and operator boundaries

The workflow registry distinguishes an implemented mutation from opening an
operator-owned control. A `supported` row needs a real typed capability and a
committed test of its observed result. A `device_handoff` row may only request
`nebula.navigate.open` or `nebula.settings.open` with an exact catalog identity.
Its observed result is **opened**. It does not mean the operator submitted the
form, accepted a permission, changed Core state, or completed the task.

Use a device handoff for interactive device input, secret entry, or a decision
whose authority belongs personally to the operator. Examples include typing in
a live terminal, controlling a visible browser with its private login state,
reviewing raw capture material, and deciding whether to grant Butler authority.
The browser owns consent and input. A model instruction cannot impersonate that
consent or turn an opening receipt into a saved-state receipt.

Ordinary nonsecret Core mutations still require typed, scoped operations with
fresh identity and revision checks and authoritative readback. Navigation alone
does not close those gaps. Where a protected workflow offers both kinds of step,
audit them separately: Butler can prepare the nonsecret operation while the
requesting browser performs the private step and checks the final result.

Every handoff row records its owner, boundary, reason, exact destination, opening
test and limited result. The guard rejects unknown destinations, mutation
operations disguised as handoffs, and handoffs labeled as supported mutations.
The inventory is a review of application entry points; it grants no additional
Core, provider, filesystem, browser or network permissions.
