# App-wide subtle polish review

Themes: Light, Dark, Zero Light and Zero Dark remain available. No palette, theme asset or theme-selection code changed. Frames below use the existing Zero Dark theme and the production DOM, with controlled fixture content. Empty states are intentional snapshots, not populated-data acceptance.

Each primary screen and Settings subsection was inspected from its real entry point. Existing minimal screens are retained rather than changed gratuitously. Add/Save/Cancel, security policy, runtime choices, recovery and approval actions retain clear labels. Familiar repeated utilities use 44px named icon controls with tooltips and keyboard focus.

| Screen | Figma frame | Review decision |
| --- | --- | --- |
| Workbench Browser | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=12-2) | Icon navigation, control state, disclosures; Assistant collapsed by default. |
| Workbench Terminal | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=15-2) | Screenshot becomes a camera icon; connection and stop/recovery retain labels. |
| Workbench Code | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=13-2) | Quiet icon toolbar for Open, Search, Find, Tasks, Problems, Format, Rename and Suggest. |
| Workbench Assistant | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=16-2) | Context, Results, Attach files, search and bookmarks use icons. |
| Workbench Files | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=14-2) | Refresh and Download use icons; Upload and Preserve as Evidence retain labels. |
| Workbench Notes | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=19-2) | Delete uses a trash icon; Save and AI transformation retain clear labels. |
| Workbench Missions | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=18-2) | Retain Start/Review labels; approvals and execution state stay explicit. |
| Workbench Activity | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=20-2) | Refresh and Copy use icons; review and cancellation remain explicit. |
| Findings | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=17-2) | Repeated row Edit becomes a pencil icon; creation and validation stay labeled. |
| Reports | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=21-2) | Retain explicit export format and sign-off labels; already grouped and consequential. |
| Library | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=24-2) | Repeated Inspect becomes an eye icon alongside existing reindex/download/remove icons. |
| Project Overview | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=23-2) | Retain grouped settings, explicit labels and disclosures; existing edit/delete/close icons are appropriate. |
| Project Assets | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=22-2) | Repeated Inspect becomes an eye icon. |
| Project Evidence | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=25-2) | Inspect and Download become icons; evidence identity remains visible. |
| Project Sources | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=26-2) | Inspect joins existing reindex/download/remove icons. |
| Settings Setup | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=28-2) | Retain grouped settings, explicit labels and disclosures; existing edit/delete/close icons are appropriate. |
| Settings Advanced | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=27-2) | Retain grouped settings, explicit labels and disclosures; existing edit/delete/close icons are appropriate. |
| Settings Diagnostics | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=29-2) | Retain grouped settings, explicit labels and disclosures; existing edit/delete/close icons are appropriate. |
| Settings Models | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=30-2) | Retain grouped settings, explicit labels and disclosures; existing edit/delete/close icons are appropriate. |
| Settings Automation | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=31-2) | Retain grouped settings, explicit labels and disclosures; existing edit/delete/close icons are appropriate. |
| Settings Project Policy | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=32-2) | Retain grouped settings, explicit labels and disclosures; existing edit/delete/close icons are appropriate. |
| Settings Identity Security | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=33-2) | Retain grouped settings, explicit labels and disclosures; existing edit/delete/close icons are appropriate. |
| Settings Release | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=34-2) | Retain grouped settings, explicit labels and disclosures; existing edit/delete/close icons are appropriate. |
| Dialog Provider | [Mockup](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=35-2) | Retain grouped settings, explicit labels and disclosures; existing edit/delete/close icons are appropriate. |

Browser concepts: [expanded](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=2-2), [collapsed default](https://www.figma.com/design/d7kSLlFfaUiDLE2GybUNqE?node-id=10-2).

Design snapshots can be exported reproducibly with `NEBULA_DESIGN_EXPORT_DIR` when running the permanent `audit every primary workspace view` Playwright test. The export removes app scripts and includes fixture data only. Capture instrumentation was added only to temporary snapshot HTML, never the shipped bundle. Native Figma components could not be imported from the advertised macOS library; the screen frames retain editable DOM-derived layers.
