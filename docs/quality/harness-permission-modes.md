# Harness permission modes

## Operator contract

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | Codex and Grok setup shows Managed and Unrestricted host access with the effect of each choice. | Harness profile, settings UI | Component, browser |
| Save and select | A saved profile immediately displays its permission mode and keeps it after refresh. | Core harness profile | Component, real Core |
| Start and use | A new Host-mode session freezes the chosen mode. Unrestricted requires project authorization Allow all, then sends Codex `never` plus `danger-full-access`, or starts Grok with always-approve and sandbox off. | Core session metadata and vendor adapter | Adapter, real Core |
| Contained project | Docker-mode sessions do not gain vendor host tools or unrestricted host execution from the profile. | Core project execution mode | Adapter |
| Approval and failure | Unrestricted Host-mode sessions do not wait for native tool approval. Unknown or disabled capabilities still fail closed. Allow all controls Nebula's enabled tools; vendor and operating-system restrictions can still reject a call. | Core permission broker and vendor | Adapter, real Core |
| Refresh, fork, reconnect | An existing session keeps its frozen choice after profile edits or reconnect. A new session uses the current profile choice. | Core session metadata | Adapter, real Core |
| Revoke | Deleting or disabling the profile follows existing harness lifecycle rules; no new revoke action applies. | Core profile store | Existing coverage |

Host access and Allow all are explicit project settings. A Host-mode session using this profile choice fails to start if Allow all is absent. Docker-mode sessions stay contained. The choice is frozen per session and does not change separate project settings, operating-system permissions, or Mercury controls. Streams, interruption, and activity recording use the existing harness path.
