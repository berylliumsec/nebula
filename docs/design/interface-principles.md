# Nebula interface principles

Agent changes must make the operator's content the visual priority. Apply these
rules to new interfaces and to the controls touched by a fix; avoid unrelated redesigns.

1. **Prefer familiar icons for secondary actions.** Refresh, close, expand,
   collapse, copy and settings normally use compact icon buttons. Reuse Lucide
   icons and existing button styles. Give every icon button a precise accessible
   name and a tooltip; hide decorative icons from screen readers.
2. **Keep text where meaning or consequences need it.** Primary actions,
   unfamiliar operations, approval decisions and destructive confirmations retain
   clear labels. Do not replace an explanation with an ambiguous symbol.
3. **Use minimal presentation.** Remove redundant labels and repeated instructions.
   Prefer one concise status line to a large card. Group related secondary actions
   near their content; avoid competing toolbars and decorative containers.
4. **Be subtle, not hidden.** Secondary controls use quiet styling, restrained
   borders and neutral colors. Reserve emphasis for the main action and actionable
   problems. Controls must remain discoverable on touch devices without hover.
5. **Disclose detail progressively.** Counts and short summaries come first;
   logs, queue contents, historical activity and advanced forms expand on demand.
   Keep failures, approvals and requests for operator input visible.
6. **Preserve usability.** Compact glyphs still need 44 px touch targets, sufficient
   contrast, visible keyboard focus and stable placement. Honor reduced motion.
   Minimalism must not remove recovery, feedback, status or accessible names.

Before handing off an interface change, inspect whether each visible word, border,
container and emphasized control helps the operator decide or act. Verify the
result under the product-quality skill, including mobile and keyboard use.

## First application: conversation catch-up

Entry: conversation read-state recovery and the catch-up summary. Replace Reload
catch-up with a refresh icon, and Dismiss catch-up with a close icon. Keep recovery
text and pending-action review visible. Core remains authoritative for read cursors;
component state owns the error and summary. Clicking refresh still retries the same
read/acknowledgment workflow, and dismissal still acknowledges the current summary.

Verification: component error/retry and dismiss behavior; committed production
browser journey with a deliberately failed catch-up response followed by real Core
recovery, keyboard/touch targets, accessible names, reload and no clipping. Use
existing desktop Chromium and mobile Chromium/WebKit profiles at LAN origin.
No harness turn or research command is executed. Physical devices unavailable.

Acceptance evidence (September 9, 2026): 2 component tests passed. The production
LAN journey at `http://192.168.1.155:19440` passed in all 8 permanent profiles:
desktop Chromium 1440/1024, emulated Android Chromium 320/390/430 and emulated
WebKit iPhone 320/390/430. Axe, icon-only copy, inline placement, 44 px geometry,
keyboard retry against real Core and reload checks passed. Production build passed.
Artifacts: `/tmp/nebula-icon-browser-final`; build: `/tmp/nebula-icon-build.log`.
Physical devices were not available. Backend behavior was unchanged.
