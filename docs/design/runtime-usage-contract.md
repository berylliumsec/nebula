# Runtime usage contract

## Operator journey

Open Settings → Advanced → Usage to inspect every configured model provider and
agent harness. Read Nebula-recorded token use separately from a vendor-reported
balance or rate limit. Refresh the vendor snapshot without starting a model turn.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | Usage is a named Settings group; configured providers and harnesses appear together, including disabled profiles. | Core catalogs and URL hash | Component and Playwright |
| Read | Recorded tokens are labelled as Nebula activity; vendor balances retain their account, key, currency and window scope. Unknown values never appear as zero. | Core turn ledger and vendor read endpoint | Python API and component |
| Refresh | Refresh updates the snapshot time and figures in place without sending a model turn. A failed read keeps local usage visible and explains retry. | Vendor read endpoint; transient UI state | Python API and Playwright |
| Reconnect | Reload shows the same profiles and recomputes local usage; balance is fetched again or served from a short Core cache. | Core database and vendor snapshot | Real Core and Playwright |
| Revoke | Deleted profiles disappear; no stale balance is shown for them. | Core profile catalog | Python API |

Create and select are existing Settings profile workflows. Streaming, interrupt,
fork and session deletion do not change this read-only view directly; the next
refresh recomputes their recorded usage. Component state owns only loading and
error presentation. The URL hash owns the open Settings group. Core owns
profile identity and the recorded turn ledger. Vendor endpoints own remaining
balance and reset claims. No balance is inferred from Nebula token counts.

The selected test layers are Python parsing/API contracts, frontend component
states, desktop/mobile browser interaction, a production build, and a real-Core
read from the Settings entry point. Physical-device verification is separate
from browser emulation.

Vendor references: [OpenRouter key](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key),
[DeepSeek balance](https://api-docs.deepseek.com/zh-cn/api/get-user-balance/),
and the installed Codex app-server `account/rateLimits/read` schema.

## Visual preview

These browser screenshots use illustrative fixture balances and token counts;
they are not reads from configured vendor accounts.

| Desktop | Mobile |
| --- | --- |
| [Model providers](previews/runtime-usage-desktop-providers.png) | [Model providers](previews/runtime-usage-mobile-providers.png) |
| [Agent harnesses](previews/runtime-usage-desktop-harnesses.png) | [Agent harnesses](previews/runtime-usage-mobile-harnesses.png) |
