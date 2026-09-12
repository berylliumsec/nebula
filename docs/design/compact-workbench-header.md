# Compact Workbench header contract

Journey: open Workbench, discover tools by named icons/tooltips, select a tool,
return to Assistant and start a new chat. Desktop tools share the shell header;
phones retain the existing bottom navigation and compact New chat action.

Invariants: one tool switcher, visible selection, keyboard arrow navigation,
44 px controls, no horizontal page clipping, focus mode retains its controls.
URL owns tool selection; existing Core/session handlers own conversations;
React owns transient menus. No persistence or API behavior is changed.

Coverage: entry/select/use and reload navigation are applicable. Chat creation
uses its existing handler. Streaming, interruption, reconnect, retry, deletion
and revocation have no changed behavior; backend lifecycle testing is excluded.
Planned layers: focused production browser regression across desktop/compact,
Android and WebKit boundary profiles; production compile. Physical devices are
unavailable. No origin-sensitive behavior is introduced.
