# Butler oversight UI contract

This is the operator-facing slice for stage 2 foundation. Core begins Watch when
a selected-project goal starts or resumes; opening Butler only reveals that
durable state. The UI never owns the watch or infers it from an open Voice session.

| Journey step | Observable invariant | State authority | Planned test layer |
| --- | --- | --- | --- |
| Open Butler in a project | Watch status, goals, and coverage are visible without another start action | Core overview and selected project URL | component, production Playwright |
| Start or resume a main or child goal | The watched list identifies the goal, session, and actor role | Core goal and overview records | real Core, production Playwright |
| Review and acknowledge an incident | An actionable incident shows evidence references, verification, impact, next action, and affected work; acknowledgment sends its saved revision and then reloads Core | Core incident and capability records | component, real Core, production Playwright |
| Review a decision | A decision shows its action, outcome, and source; unavailable history is explicit | Core decision and capability records | component, real Core |
| Review resources | Persisted OpenRouter balances and harness quota windows show their source, observation time, and threshold; unavailable balances remain unknown | Core project overview and provider or harness reports | component, real Core, production Playwright |
| Change authority | Watch is the default; Steer or specific Delegate grants appear only after Core saves and reloads the project setting | Core project setting and grant revision | component, real Core, production Playwright |
| Switch projects | The old project's overview disappears immediately | selected project URL and Core overview | component, production Playwright |
| Refresh, reconnect, or return to tab | A fresh overview replaces stale data without duplicate rows; errors leave a Retry action | Core overview; React owns only loading and error presentation | component, real Core, production LAN Playwright |
| Review a long project | Core totals remain visible and the operator can page beyond the first 50 goals, incidents, or account observations | Core overview offsets and summary | component, real Core |

The UI uses Core's project overview and bounded pages. It refreshes on project
selection, reconnection, tab visibility, an explicit operator request, and every
15 seconds while visible. A later project-scoped Core change stream can replace
the timer; the foundation endpoint does not yet provide a push signal. No browser
storage holds incidents, decisions, or oversight authority. Links are derived
from the selected project and saved session IDs. Child rows name their parent
main goal. Butler actions go through the parent main agent; the UI offers only
inspection links for child work.

The component is mounted in the Butler panel. The production LAN browser cases
exercise that entry point against real Core instances; component tests cover
the narrower rendering and interaction states.
