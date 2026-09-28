# OpenRouter profile routing policy

Baseline: `origin/main` at `23aef109029bfe29afc35dd2c79bccd3ba2b22b9`.

## Operator contract

The entry point is Settings → Model providers → edit an OpenRouter profile.
An operator can require provider-side Zero Data Retention (ZDR) and select the
OpenRouter upstream providers allowed to serve that profile. Both controls are
optional. Core's durable `ProviderProfile.metadata.options` owns the saved
policy; the dialog owns only an unsaved draft. OpenRouter owns current endpoint
eligibility, and its response owns the route actually selected.

| Journey step | Observable invariant | Authority | Planned test layer |
| --- | --- | --- | --- |
| Discover and edit | The current ZDR choice and upstream allowlist appear in the profile dialog | Core profile → UI | component and selected Playwright |
| Save and refresh | Saved choices reappear after list reload and dialog reopen | Core database | real Core |
| Select and use | ZDR opt-in sends `provider.zdr=true`; allowed upstreams send `provider.only`; unset options add neither field | Core profile → OpenRouter payload | focused Python and live provider |
| Model discovery | Account-visible models are narrowed to routes satisfying the saved policy; failed eligibility checks cannot silently broaden the list | OpenRouter account catalog and endpoint policy | focused Python and real Core |
| Route limits | The model's usable endpoint set excludes routes outside the saved policy | OpenRouter endpoint policy | focused Python |
| Failure and retry | An unavailable eligible route reports failure without falling back outside the policy | OpenRouter and Core | focused Python and real Core |

Streaming, cancellation, reconnect, and deletion retain their existing lifecycle;
the profile policy is read for every provider request. The UI must preserve the
saved policy on edit, refresh, and reconnect. No ZDR choice or upstream is
hardcoded into Nebula's default behavior. The local profile change is a separate
post-deployment action and will not be committed to the public repository.

Selected validation is recorded in `.github/test-selection.json`: seven Python
policy cases, twenty-one frontend cases across the Settings dialog and provider
cache, three mocked settings journeys (desktop Chromium, mobile Chromium,
mobile WebKit), and one production-bundle real-Core LAN journey. The real-Core
test saves the policy through Settings and checks Core persistence and browser
reload. The adapter's request policy and fail-closed endpoint discovery are
tested with exact mocked OpenRouter responses. A live billable OpenRouter chat
under this policy and a physical mobile browser remain separate checks before
claiming those paths verified.
