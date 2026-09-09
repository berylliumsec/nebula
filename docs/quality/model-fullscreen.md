# Model panel expansion

Entry: Model → expand Objects, Relationships or Model inspector. The same panel
fills the browser viewport. Restore or Escape returns to the inline layout.
Selection from an expanded outline or map opens the expanded inspector.

Core owns graph data and revisions; URL parameters own selection, depth and
filters; React owns temporary expansion. Existing content stays mounted across
expansion, retaining editor drafts, disclosures and graph state. The background
is inert while expanded, focus stays within the panel, and Restore receives
focus after closing. Errors leave expansion so the existing inline recovery is
reachable. Cancelling a new editor does not hide an expanded inspector.

Lifecycle: discovery, selection, edit, restore, failure/retry and refresh apply.
Reload preserves URL selection/filters and starts inline. Expansion creates no
durable entities; deletion/revocation and streaming/interrupt protocols are
unchanged. No Fullscreen API or secure-origin requirement is introduced.

The broader product rule: dense model panels need a discoverable way to use the
available screen without losing the operator's context or drafts.

## Verification

- `npm --prefix ui test -- src/pages/ApplicationModelPage.test.tsx`: 8 passed.
- `npm --prefix ui run build`: passed (existing chunk-size/dynamic-import warnings).
- Focused browser file: `tests/application-model-responsive.spec.ts`, using
  `ui/playwright.application-model.config.ts`. Production bundle and disposable
  real Core at `http://192.168.1.155:19567`; paired browser authentication.
- Browser matrix: desktop Chromium 1440/1024; emulated Android Chromium and iPhone
  WebKit at 320/390/430: all 8 runs passed in 1.4 minutes. Checks cover fullscreen bounds, 44 px restore target,
  keyboard focus, Escape, axe, selected-item inspection, draft retention, saved
  model selection/filter reload and mobile landscape.
- Screenshots/traces: `/tmp/nebula-model-fullscreen-verified`.
- Physical devices/software keyboards: unavailable, not verified.
- No live deployment or live provider calls are part of this change.
