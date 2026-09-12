# Harness account homes

Operator journey: Settings → Agent harnesses → add a named account profile → browse/create a host account folder → sign in on the host with the displayed command → Check → select the named harness in Assistant. Separate profiles can use one executable.

| Step | Invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover/create | Optional account folder has a host browser and persists after save/reload | Core HarnessProfile | component + real Core production browser |
| Sign in/check/retry | Launch and discovery use the same explicit vendor home; missing login remains recoverable using the displayed host command | vendor credential store + Core health | adapter fixtures; real vendor login requires user credentials |
| Select/use | Named profiles appear in existing Assistant selector; project cwd does not change | Core profile and chat | real Core fixture journey |
| Stream/interrupt | Existing transport and cancellation behavior unchanged | harness session | focused adapter regressions |
| Refresh/reconnect | A used profile cannot be rebound to another home | Core profile/session references | validation tests + real Core |
| Fork/switch | Switch via a distinct named profile; existing next-turn session handoff preserves transcript | Core chat | existing settings regression |
| Delete/revoke | Existing referenced-profile deletion guard remains; profile removal does not remove vendor credentials | Core references/vendor | API validation |

Account directories are optional and apply only to spawned Codex/Grok. Default profiles retain the host default; explicit account homes never change OS HOME or project cwd. Codex explicit homes use file credential storage consistently in login instructions and launches. The UI offers terminal sign-in on the Nebula host, not a browser-device terminal. Remote endpoints keep their server-managed credentials. No vendor logins are automated during acceptance and no existing credentials are read or copied.

Broader rule: every account-dependent operation must resolve one profile-owned home, rather than independently using process defaults. Host-folder selection remains distinct from project-folder selection.

## Acceptance evidence

Focused Python: 19 passed locally on Python 3.11; CI selects Python 3.12. Components: 10 passed (HarnessSettings and HostFolderPicker). Production bundle, Ruff, mypy and both diagnostic audits passed. Real-Core production LAN account creation, folder selection, saved-profile reload, account switching within a chat, used-account mutation rejection, Core restart/reconnect, and the accessible queue icon passed across Chromium desktop 1440/1024, emulated Android Chromium 320/390/430 and emulated iPhone WebKit 320/390/430. Automated dialog accessibility, clipping, keyboard focus and 44 px queue targets are asserted. Browser tests retain account-dialog and queue screenshots in test output.

Limitations: no physical-device keyboard/rotation/background-network transitions, no live vendor sign-in/concurrent vendor sessions, and no deployment or package-install verification. Vendor protocol execution uses inert fixtures; real Core storage, authenticated LAN HTTP and production UI are exercised. This is partially verified account support until live accounts are exercised.
