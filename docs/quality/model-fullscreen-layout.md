# Fullscreen graph layout

Contract before implementation: from Project > Model, select a neighborhood and
expand Relationships. The graph itself, not only its containing panel, must use
the available width and viewport height. Preserve readable cards and scrolling
for dense graphs, selection, depth, keyboard focus, and Escape/restore behavior.

Core owns objects/relationships; URL owns selection/depth; component state owns
expanded presentation and measured geometry. No graph data or schema changes.
Discover/select/expand/resize/restore/refresh are required; create/delete/retry
retain the existing Model regressions. Streaming, revocation, fork and provider
sessions are not applicable to this presentation-only fix.

Planned tests: graph layout unit tests including a 2800px container, Model-page
component regressions, and the existing dense real-Core production LAN journey
in desktop Chromium 1440/1024 and emulated Android Chromium/iPhone WebKit at
320/390/430. Exercise a wide resize inside the desktop journey, geometry of the
nodes as well as the dialog, edges, keyboard, focus, landscape and reload.
Physical devices unavailable. Preserve the preceding uncommitted schema work.

## Evidence — September 9, 2026

Cause: three-column maximum and fixed 280px spacing persisted inside a full-width
panel. The new measured layout balances rows/columns against viewport dimensions,
distributes columns across the available width and expands row spacing into the
fullscreen graph viewport. Card dimensions remain readable; dense graphs scroll.

- `npm test -- src/components/ApplicationModelGraph.test.tsx src/pages/ApplicationModelPage.test.tsx`: 20 passed.
- `npm run build`: passed; existing chunk-size/dynamic-import warnings only.
- `npx playwright test --config playwright.application-model.config.ts application-model-responsive.spec.ts --max-failures=1`: 8 passed in 1.5 minutes.
- Real Core with disposable data and the production bundle at
  `http://192.168.1.155:19442`; desktop Chromium 1440/1024 (both resized to
  2800x1400 and restored), emulated Android Chromium and iPhone WebKit
  320/390/430. Node spread, dialog bounds, arrow geometry, selection, keyboard,
  focus, Escape/restore, landscape, reload, and axe checks passed.
- Inspected the ultrawide screenshot. Artifacts: `/tmp/nebula-fullscreen-layout`.
- `git diff --check` passed; the focused test-selection receipt was validated.

The nebula-product-quality skill required checking the graph's actual content
geometry, not merely the fullscreen dialog boundary. No backend change in this
increment; previous schema checks were not rerun. No physical device test, live
deployment, commit or push. Existing live graph data remains untouched.
