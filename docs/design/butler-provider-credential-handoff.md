# Butler provider credential attachment

Entry point: an operator asks Butler to attach a Nebula-managed credential
reference to an existing model provider. Butler can discover the provider and
its current revision through the Settings API. The requesting browser shows the
provider name, credential slot, and exact reference, then asks for confirmation.
The secret value stays in the credential store and never enters the model.

| Journey step | Observable invariant | State authority | Check |
| --- | --- | --- | --- |
| Discover/select | Butler reads a current provider identity and revision; only an exact selected profile may be changed | Core provider store; selected project in browser | Capability and component tests |
| Create/use | The browser confirms the reference is available, confirms the target and slot, and patches only that slot at the current revision | Core credential and provider stores; browser confirmation | Component and real-Core browser |
| Read back | A fresh provider read returns the exact reference and a newer revision; a fresh credential status remains available | Core stores | Component and real-Core browser |
| Refresh/reconnect | The provider retains the reference in Settings and Core after reload | Core store and Settings view | Real-Core browser |
| Failure/retry | Stale provider revisions fail before mutation; a lost write response triggers a read of the exact profile before another attempt | Core revision and browser recovery | Component and real-Core browser |
| Delete/revoke | The existing exact-reference credential deletion handoff remains separate; deleting a referenced credential can make the provider unavailable | Credential store and provider health | Existing credential journey |

Changing an inference reference invalidates existing tool-capability
verification; the operator must refresh provider health and verify tools before
using that profile for Butler or another tool-enabled agent.

The provider is global in the current single-organization Core. The requesting
browser's selected project authorizes the handoff context, while Core owns the
global profile. The action is interactive only. Management credentials are
accepted only for OpenRouter and never placed in the inference slot. The
browser sends no raw secret, cookies, or credential-store material to Butler.
Streaming and interruption are not applicable to this short Core mutation.
