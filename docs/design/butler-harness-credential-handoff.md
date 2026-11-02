# Butler harness credential attachment

Entry point: an operator asks interactive Butler to attach an available
Nebula-managed credential reference to an existing Assistant harness. Butler
pages nonsecret harness identities and current revisions. The requesting
browser shows the exact harness and reference, then asks for confirmation.
Core stores only the reference on the profile; the credential value remains in
the credential store and never enters the model transcript.

| Journey step | Observable invariant | State authority | Check |
| --- | --- | --- | --- |
| Discover/select | Butler reads an exact global harness ID, name, connection mode and revision | Core harness profile; selected browser project | Capability and component tests |
| Create/use | The browser confirms the reference is available, then patches only `auth_mode` and `secret_ref` at the current revision. Spawned harnesses use `secret_ref`; Codex endpoints use `endpoint_bearer` | Core profile and credential store; browser confirmation | Component and real-Core browser |
| Read back | A fresh harness read returns the exact reference, authentication mode and newer revision; credential status remains available | Core stores | Component and real-Core browser |
| Refresh/reconnect | The saved harness authentication survives browser reload, and the Settings list refreshes or exposes its retry control | Core store; WorkspaceContext presentation | Real-Core browser |
| Failure/retry | Stale revisions and unsupported harness authentication are refused before write; a lost response triggers a read of the exact profile before retry | Core conflict contract and browser | Component and real-Core browser |
| Delete/revoke | Existing exact-reference credential deletion stays separate; a referenced harness may stop authenticating after deletion | Credential store and harness health | Existing credential journey |

Grok ACP uses its cached vendor login and cannot accept a managed reference.
Claude Agent SDK profiles with native shell require existing-session
authentication; the handoff refuses them. Running harness sessions retain their
own process/session state; the operator must check the profile's health before
using it for a new turn. Streaming and interruption do not apply to the short
profile mutation.
