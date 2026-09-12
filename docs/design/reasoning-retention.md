# Reasoning retention contract

Branch: codex/reasoning-display. Entry: project Workbench → chat → Show activity → reasoning episode.

| Journey | Observable invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover/select | Thinking remains discoverable, including when a summary was not supplied | Core activity; UI disclosure | component + production browser |
| Stream/use | Every supplied displayable summary and commentary fragment remains readable, including text beyond 64K | harness adapter + Core ledger | adapter + reducer |
| Complete | Final summaries do not erase differing streamed summaries | Core events; UI projection | reducer + browser |
| Interrupt/failure/retry | Recorded text survives a stopped turn; absent text is explained in place | Core ledger | existing stopped-turn browser fixture + reducer |
| Refresh/reconnect/background | Replaying ordered events gives the same text without duplicated chunks | Core ledger; URL session identity | runtime persistence + reducer + real-Core browser reload |
| Long content/mobile | Full text is selectable and scrollable without horizontal clipping | UI presentation | desktop 1440/1024, emulated Chromium/WebKit 320/390/430 |
| Create/fork/delete | No change to session mutation or lineage | Core | not applicable to this read/display fix |

Provider-designated Codex summaries remain the display contract; raw private reasoning is not retained. Summary text is sanitized/redacted like other display text. Diagnostic/tool payload bounds remain in force. Historical truncated records cannot be reconstructed: mark known truncation clearly. Details stay collapsed by default under interface principle 5; show reasoning counts so the disclosure is discoverable. Preserve streamed text separately only when the provider's completed snapshot differs.

Planned focused layers: Codex/Grok adapter reasoning contracts, durable replay for both vendors, activity reducer/model/components, and the existing real-Core thinking journey with synthetic saved events on the production LAN bundle. No provider tasks or tools run in the browser fixture. Physical devices and live vendor turns are separate evidence, not claimed by fixtures.

Codex now requests `detailed` summaries, supported by the bundled 0.144.0 TurnStartParams schema and [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference#model_reasoning_summary). Actual summary availability remains model/provider controlled.
