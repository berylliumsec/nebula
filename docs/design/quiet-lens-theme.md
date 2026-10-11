# Quiet Lens theme study

Quiet Lens is the first-run default. A saved appearance choice on the device
continues to take precedence. The README hero image is a production UI capture
at a LAN origin with illustrative conversation text; its controls are in their
resting state.

Status: proposed default theme in the real Nebula UI, submitted for design and
code review. The earlier Figma frames remain an editable record of the concept;
the live Workbench and README capture show the current interaction.

[Open the Figma file](https://www.figma.com/design/S5skJUufDSwOKyhSWw0C9u).

This study follows the user's sketches: the top menu and left sidebar are two
separate, closed, tapered shapes. Their ends do not touch. After mouse inactivity,
their contours recede. Mouse movement reveals thin top, left, and right shapes;
hovering one reveals only that surface's contents. The bottom composer has its
own thin outline and opens on hover.
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
| Desktop / Idle | `10:84` | [Preview](quiet-lens-previews/desktop-idle.png) | Earlier idle proposal, before the composer and Session rail visibility refinement. |

All eight frames are native Figma layers with editable Geist text, instances of
five local two-state component sets, and two composer components. The file has
local dark-mode color variables and text styles. The exported previews are only
for quick review; they are not the editable source. The latest composer and
Session rail visibility decision is implemented in the real Core review below;
the eight Figma frames have not yet been revised to match this iteration.

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
| Mouse movement, composer at rest | [Thin Session outline](quiet-lens-previews/real-core-rail-active-composer-idle.png) |
| 60-second pointer idle | [Session rail hidden, thin composer outline](quiet-lens-previews/real-core-rail-idle-composer-idle.png) |
| Composer hover | [Composer revealed](quiet-lens-previews/real-core-composer-hover.png) |
| Session outline hover | [Session contents revealed](quiet-lens-previews/real-core-rail-hover.png) |
| 320 px touch composer | [Multiline draft visible](quiet-lens-previews/real-core-mobile-composer-visible.png) |

The current visibility proposal for design review is: keep conversation content
and anything requiring operator attention visible. A thin tapered outline marks
the composer at rest. Its controls appear while its own area is hovered or it
has keyboard focus or active typing; moving the mouse elsewhere does not reveal
them. The Session rail begins hidden. Mouse movement anywhere shows its thin
vertical outline; hovering that outline or reaching the rail by keyboard focus
reveals its contents. The outline fades after 60 seconds without movement or
hover. Its pending-action state remains visible. Pointer movement also brightens
the independent top and left navigation contours; it does not expand labels.
Hovering or focusing a contour reveals that menu alone. A tap opens its menu on
touch screens, and Escape or an outside tap closes it. The side menu owns project
and section navigation; the top menu owns page actions, status, and commands.
Primary buttons use the tapered oval language while keeping their labels readable.

## Operator journey and interaction contract

| Journey step | Observable invariant | State authority |
| --- | --- | --- |
| Open a conversation | The content remains the visual priority; the two navigation lenses stay disconnected. A thin composer outline remains at the bottom, with its full controls revealed by hovering or focusing it. | Core owns the saved conversation; the URL owns the selected project and conversation; React owns unsent text. |
| Move the mouse | The top and left contours brighten and a thin right Session outline appears. The rail fades after 60 seconds without movement or hover. Moving outside the composer does not reveal its controls. | Local presentation state and a 60-second timer. |
| Use the composer | Hovering its outline and stable footprint or reaching it by keyboard focus reveals the full controls. While focused, typing, sending, or resolving an action, the composer remains visible. A long unsent draft is preserved if focus leaves. | React owns the draft and presentation; Core owns submitted turns. |
| Use the Session rail | Hovering the narrow right outline or focusing a rail control reveals its links. A pending action keeps them visible. Hiding the rail does not change the selected conversation or shift its layout. | Core owns goal and agent status; React owns transient visibility. |
| Reveal navigation | Hover, keyboard focus, or tap reveals only the targeted lens without moving the content; Escape closes a tapped lens. | Local presentation state. |
| Select a destination | A destination has a readable label, visible focus, and a usable touch target of at least 44 px despite the thin visual contour. | URL plus Core catalog. |
| Send a message | Send and its enabled or disabled state are clear when the composer is revealed. An active response keeps its Stop control visible. | Core owns the submitted turn; local state owns unsent input. |
| Handle an approval or failure | Items requiring action stay explicit and visible even when navigation is collapsed. | Core and provider or harness session. |
| Reload or reconnect | The selected conversation and transcript return; lens reveal state may reset. | URL and Core, with local presentation state transient. |

### Focus mode refinement

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Enter focus mode from Workbench | Quiet Lens replaces the full-width focus toolbar with a thin top outline. The selected view and its content stay in place. | React owns the temporary focus state; the URL owns project, view, and conversation identity. | Mocked desktop and mobile browser; real Core visual review. |
| Reveal focus controls | Hovering the top outline reveals the existing tabs and actions. Keyboard focus reveals them too, and the exit control remains operable. Pointer movement elsewhere does not expand them. | React owns focus state; CSS owns transient presentation. | Desktop pointer, keyboard, and accessibility checks. |
| Use or leave focus mode | Switching views still works, Escape and Exit focus mode close focus, and the prior work surface returns without losing the unsent draft. Touch keeps controls visible because it has no hover. | URL and Core own the selected conversation and transcript; React owns unsent input and focus state. | Desktop and phone browser journeys, plus real Core. |
| Refresh or reconnect | Focus may reset, while the selected view and saved conversation return. The theme remains selected on this device. | URL, Core, and device theme storage. | Real Core reload. |

Creation, streaming, interruption, retry, and deletion use the existing Workbench
and Core controls; this focus-toolbar change does not alter those operations.
Approval and failure messages in the work surface remain visible while the
toolbar is minimized. Test the production bundle at desktop 1440 and 1024 px,
emulated 320–430 px Chromium and WebKit phone widths, and the disposable LAN Core.

Lifecycle scope: discover theme in Appearance; select and persist it; navigate,
focus, and use existing actions; create and select a conversation; type a draft,
send, stream, interrupt, retry, and reconnect; reload and restore the theme;
handle failures and approvals. Fork and deletion keep their existing controls
and Core paths, with no server state added by this theme. Backgrounding pauses
pointer movement, so the rail may fade; a fresh movement reveals it again.
The URL and Core remain authoritative for navigation and work, browser storage
for device theme, and React state for transient reveal and unsent input. Touch
devices keep the composer and available navigation visible because hover is
unavailable.

Planned test layers for the new visibility rule: the existing focused Quiet Lens
Playwright journey in desktop Chromium and mobile Chromium/WebKit at 320–430 px,
with a clock-controlled 60-second idle assertion on desktop; selected App and
ThemeContext component files; a production build; and the disposable real-Core
review on a LAN origin. Check hover locality, keyboard focus and draft retention,
rail outline/hover/idle reveal, pending-action visibility, long content, touch targets,
reduced motion, and reload. A review build is not a claim of full product
acceptance; skipped gates must be recorded in the handoff.

## Review evidence and limits

The production UI bundle builds. The focused Quiet Lens browser journey passed
in eight profiles before the right Session outline refinement: desktop and
compact Chromium, plus Chromium and WebKit at 320, 390, and 430 px. The updated
wide desktop journey passed again after that refinement. It selects the theme
through Appearance, checks separate
top and side reveals, Escape, 44 px targets, no horizontal overflow, and visible
three-line and scrollable long drafts. At desktop widths it checks composer
hover locality, keyboard focus, and draft retention after blur. The wide desktop
profile checks the Session rail's initial hidden state, 59-second and 60-second
timer boundary, movement reveal as a 12 px contour, narrow hover target, full
contents on hover, keyboard focus, and pending-action visibility.
The real LAN Core review reached
Workbench, Findings, Reports, Projects, Library, and Settings; it clicked the
Findings primary action, sent a chat message through Core, and reloaded the saved
conversation. In this iteration a second message was sent from a newly created
chat with the hover-revealed composer and restored after reload. At the LAN
origin, wide Chromium showed a visible 12 px composer outline and hidden controls
outside its hover area, visible controls on hover, and a separate 12 px Session
outline whose contents remained hidden until its narrow hit area was hovered.
The rail changed from opacity 1 to 0 at the simulated 60-second boundary and
back to 1 on pointer movement. The 320 px Chromium and
390 px WebKit real-Core browser checks kept the multiline composer visible with
zero horizontal overflow. These are browser emulations, not physical phones.
A two-worker replay briefly reset the WebKit 430 px fixture to Zero Classic;
the same profile passed alone, and a serial replay passed all eight profiles.

The selected App/ThemeContext unit files have 38 passes and eight failures in
other App cases; their baseline status has not been established. A physical
phone, end-to-end approval/error/retry paths, and a full provider or runner
workflow have not been exercised. This remains a design-review prototype, not
a completed product theme.

## Decisions for design review

- Whether the resting contours are visible enough to discover without visual noise.
- How wide the side lens should become when revealed, especially on mobile.
- Whether mobile navigation should obscure, dim, or shift the conversation while open.
- Which actions should use the lens shape, and which require persistent text because
  they carry consequences or request operator input.
- Whether the thin composer and Session outlines are discoverable enough, and
  whether the blank right gutter should remain to avoid moving the transcript.
