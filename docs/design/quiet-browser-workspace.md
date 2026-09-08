# Quiet browser workspace

Design: https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=2-2

The operator opens Workbench > Browser, navigates and captures context, then asks
and follows up beside the page. Recognizable toolbar and composer actions use
icons, accessible names and tooltips. Control ownership and errors stay visible;
secondary viewport, file and protected-value tools use progressive disclosure.
The Assistant starts collapsed, opens explicitly or when attaching context, and
returns focus to its toggle when closed. Closing it does not delete conversation state.
The existing Geist font and theme tokens remain authoritative.

| Journey | Invariant | Authority | Validation |
| --- | --- | --- | --- |
| Discover/select | Browser engine, tab, address and Assistant remain discoverable | Core tabs, local engine preference, URL conversation | component + production Playwright |
| Navigate/capture | Named icon actions retain handlers and previews | Core browser + transient capture | component + real Core UI |
| Create/use | New tab, files, credentials and context remain usable | Core | existing real Core workflow |
| Stream/interrupt | Composer actions and approval stay reachable | Core/harness | production UI + real Core |
| Refresh/reconnect | Saved tabs and conversation return | Core + URL | existing real Core workflow |
| Failure/retry | Error and retry remain visible | Core error + connection | component + production UI |
| Delete/revoke | Existing remove/close behavior preserved | Core | component + existing lifecycle coverage |
| Responsive/accessibility | 44px targets, named icons, focus, no clipping | presentation | Chromium/WebKit 320/390/430, desktop 1024/1440, landscape + Axe |

Testing uses a production bundle and LAN origin, distinguishes API fixtures from
real Core, and does not claim physical-device coverage from emulation. No new
state authority or protocol behavior is introduced. The broader product rule is
to group controls by purpose and use named icon actions for familiar operations,
while keeping decisions and recovery readable.

Figma discovery: no Code Connect files or existing screens. macOS toolbar library
was discovered but import was denied; custom editable layout uses Geist and
restrained neutral colors. Library background variables were not returned.

## Validation record

- Production build passes; bundle index SHA256
  `6dd82ee10a8c9fa5b54c5487d14b674b58f25b737484ff022d8900b4499380fe`.
- Focused browser/notes/files/activity components: 23 passed. Editor and screenshot
  components: 25 passed. Diagnostic audit: no unclassified catches.
- Real Core + live Codex + headed Chromium 149.0.7827.55 passed at 1440x900 and
  1024x700, production LAN `http://192.168.1.155:53035`. The journey covers selection,
  context attachment, answer, inline approval, visible page change, reload,
  reopening the collapsed Assistant, and the same conversation in main chat.
  Log: `/tmp/nebula-quiet-core-compact-fixed.log`.
- Browser fixture checks include icon names, tooltips, 44px targets, search
  keyboard disclosure, collapsed default/reopening, file approval, image capability,
  long context/transcript, no overflow and Axe. Final multi-engine counts and local
  deployment evidence are retained in [PR #259](https://github.com/berylliumsec/nebula/pull/259).
  Physical phones were not used.
- `audit:css` still reports the pre-existing 9px IP metadata label in `base.css:142`,
  also present in origin/main. This change does not edit that rule.
- Initial test-launch stalls occurred before page creation with the default cached
  Chromium. The qualified Chromium runtime under
  `/tmp/nebula-companion-validation/playwright-browsers` starts promptly. Earlier
  broad fixture assertions also assumed pre-canonical project URLs; they now accept
  only the exact requested legacy URL or its exact scratch-project canonical URL.
- The first compact real-Core run exposed Send clipping beneath status banners.
  The final composer reserves its intrinsic height and uses an icon-only runtime
  selector in narrow browser panes; the repeated real-Core run passed.

- The 24-screen WebKit capture journey reached 22 captures before its 150-second
  total deadline on this software-rendering host. It now has a 300-second total
  budget; individual interaction, accessibility and geometry assertions are unchanged.
- The operator also authorized updating the local server. Publish the verified
  production assets before atomically replacing its entry page; retain prior hashed
  assets for open sessions. This presentation-only deployment needs no Core restart.
