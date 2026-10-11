# Quiet Lens theme study

Status: throwaway, opt-in theme in the real Nebula UI for design review. No PR or
release is proposed until the interaction choices below are reviewed.

[Open the Figma file](https://www.figma.com/design/S5skJUufDSwOKyhSWw0C9u).

This study follows the user's sketches: the top menu and left sidebar are two
separate, closed, tapered shapes. Their ends do not touch. After mouse inactivity,
their contours recede. Mouse movement reveals the two thin shapes and quiet
secondary controls; hovering either shape reveals only that shape's labels.
Keyboard focus has the same reveal as hover. Touch keeps visible handles and
opens a lens with a tap. The Send control and composer use the same contour
language.

## Review frames

| Frame | Figma node | Preview | Intent |
| --- | --- | --- | --- |
| Desktop / Pointer active | `4:6` | [Preview](quiet-lens-previews/desktop-rest.png) | Mouse movement reveals two independent, thin outlines. |
| Desktop / Top hover | `5:33` | [Preview](quiet-lens-previews/desktop-top-hover.png) | Top lens reveals project destinations. |
| Desktop / Side hover | `5:66` | [Preview](quiet-lens-previews/desktop-side-hover.png) | Side lens reveals contextual destinations. |
| Desktop / Action focus | `5:98` | [Preview](quiet-lens-previews/desktop-action-focus.png) | Send action expands from a small lens. |
| Mobile / Rest | `6:75` | [Preview](quiet-lens-previews/mobile-rest.png) | The same two shapes remain separate at 390 px. |
| Mobile / Top tap | `6:99` | [Preview](quiet-lens-previews/mobile-top-tap.png) | Touch reveals the top menu. |
| Mobile / Side tap | `6:129` | [Preview](quiet-lens-previews/mobile-side-tap.png) | Touch reveals the side menu above subdued content. |
| Desktop / Idle | `10:84` | [Preview](quiet-lens-previews/desktop-idle.png) | Mouse inactivity recedes both contours while content and composer remain. |

All eight frames are native Figma layers with editable Geist text, instances of
five local two-state component sets, and two composer components. The file has
local dark-mode color variables and text styles. The exported previews are only
for quick review; they are not the editable source.

## Real Nebula review

Select Quiet Lens under Settings → Appearance in a disposable Core instance.
The shell, route content, project navigation, Core status, approvals, and chat
composer are the actual Nebula components. The disposable model returns clearly
labelled placeholder replies, while Nebula Core owns the conversation and
persists it across reloads. The local review link is shared separately so its
temporary access token is not committed.

| Real Core view | Capture |
| --- | --- |
| Desktop multiline draft | [Chat composer](quiet-lens-previews/real-core-chat-multiline.png) |
| Desktop empty chat | [Workbench](quiet-lens-previews/real-core-chat-rest.png) |
| Desktop top lens over Findings | [Top menu](quiet-lens-previews/real-core-top-open.png) |
| Desktop side lens over Workbench | [Side menu](quiet-lens-previews/real-core-side-open.png) |
| Findings and project overview | [Findings](quiet-lens-previews/real-core-findings-rest.png) · [Projects](quiet-lens-previews/real-core-projects-rest.png) |
| Appearance picker | [Quiet Lens selected](quiet-lens-previews/real-core-settings.png) |
| Settings setup | [Settings](quiet-lens-previews/real-core-settings-rest.png) |
| 320 px chat with three visible lines | [Mobile chat](quiet-lens-previews/real-core-mobile-chat.png) |
| 320 px side lens | [Mobile navigation](quiet-lens-previews/real-core-mobile-side.png) |

The visibility proposal is: at rest, keep content, the composer, a relevant
primary action, and anything requiring operator attention visible. Pointer
movement brightens the two independent contours; it does not expand labels.
Hovering or focusing a contour reveals that menu alone. A tap opens its menu on
touch screens, and Escape or an outside tap closes it. The side menu owns project
and section navigation; the top menu owns page actions, status, and commands.
Primary buttons use the tapered oval language while keeping their labels readable.

## Operator journey and interaction contract

| Journey step | Observable invariant | State authority |
| --- | --- | --- |
| Open a conversation | The content and composer remain the visual priority; the two navigation lenses stay disconnected, and secondary chrome recedes after mouse inactivity. | Core owns the saved conversation; the URL owns the selected project and conversation. |
| Move the mouse | Both thin contours and secondary controls become discoverable without revealing their labels or shifting content. | Local presentation state. |
| Reveal navigation | Hover, keyboard focus, or tap reveals only the targeted lens without moving the content; Escape closes a tapped lens. | Local presentation state. |
| Select a destination | A destination has a readable label, visible focus, and a usable touch target of at least 44 px despite the thin visual contour. | URL plus Core catalog. |
| Send a message | Send and its enabled or disabled state remain clear at rest and do not obscure the composer. | Core owns the submitted turn; local state owns unsent input. |
| Handle an approval or failure | Items requiring action stay explicit and visible even when navigation is collapsed. | Core and provider or harness session. |
| Reload or reconnect | The selected conversation and transcript return; lens reveal state may reset. | URL and Core, with local presentation state transient. |

Lifecycle scope: discover theme in Appearance; select and persist it; navigate,
focus, and use existing actions; reload and restore the theme; handle failures
and approvals. Conversation creation, streaming, interrupt, retry, fork, and
deletion continue through the existing Core paths; this theme adds no server
state. The URL and Core remain authoritative for navigation and work, browser
storage for device theme, and React state for transient lens presentation.

Planned test layers: ThemeContext and shell component behavior; focused
Playwright journeys in desktop Chromium and mobile Chromium/WebKit at 320–430 px;
production build; and a disposable real-Core review at loopback and LAN origin.
Check multiline composer input, long content, focus, touch targets, reduced
motion, reload, and pending-action visibility. A review build is not a claim of
full product acceptance; skipped gates must be recorded in the handoff.

## Review evidence and limits

The production UI bundle builds. The focused Quiet Lens browser journey passes
in eight profiles: desktop and compact Chromium, plus Chromium and WebKit at
320, 390, and 430 px. It selects the theme through Appearance, checks separate
top and side reveals, Escape, 44 px targets, no horizontal overflow, and visible
three-line and scrollable long drafts. The real LAN Core review reached
Workbench, Findings, Reports, Projects, Library, and Settings; it clicked the
Findings primary action, sent a chat message through Core, and reloaded the saved
conversation. At 320 px, the three-line draft had matching 87 px client and
scroll heights with zero horizontal overflow.
A two-worker replay briefly reset the WebKit 430 px fixture to Zero Classic;
the same profile passed alone, and a serial replay passed all eight profiles.

The two changed unit assertions pass. The selected App/ThemeContext unit files
as a whole have 37 passes and nine failures in other App cases; their baseline
status has not been established. A physical phone, approval/error/retry paths,
and a full provider or runner workflow have not been exercised. This remains a
design-review prototype, not a completed product theme.

## Decisions for design review

- Whether the resting contours are visible enough to discover without visual noise.
- How wide the side lens should become when revealed, especially on mobile.
- Whether mobile navigation should obscure, dim, or shift the conversation while open.
- Which actions should use the lens shape, and which require persistent text because
  they carry consequences or request operator input.
