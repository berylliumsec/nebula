# Nebula 3 usage scenarios

The Playwright usage suite records realistic operator workflows in Nebula's
Zero Layer theme and converts them to named H.264 MP4 videos. It is separate
from the visual-regression and acceptance suites because its output is intended
for product review and documentation, not source control.

Run all scenarios from the repository root:

```bash
npm --prefix ui run record:usage
```

Recordings are written to `ui/usage-videos/`. The directory is intentionally
gitignored. A successful rerun replaces the video for each scenario with the
same stable filename and removes Playwright's intermediate WebM files.

## Scenario and feature coverage

| Video | Real operator scenario | Features exercised |
| --- | --- | --- |
| `01-create-an-authorized-security-project.mp4` | Start an authorized Northstar test-API assessment | Project creation, project switcher, Overview |
| `02-use-the-isolated-kali-terminal.mp4` | Inspect the Kali environment and hash a project target file | Terminal startup, project workspace, selective audit boundary |
| `03-create-code-and-review-project-files.mp4` | Write a repeatable security-header check and review the saved file | Code editor, save, Files preview |
| `04-browse-an-authorized-target-in-the-desktop-shell.mp4` | Open the authorized target and its runbook in isolated tabs | Desktop Browser, address normalization, project-isolated tabs |
| `05-ask-the-local-assistant-with-cited-project-knowledge.mp4` | Ask for remediation priorities grounded in the rules of engagement | Assistant, local provider, knowledge retrieval, citations |
| `06-capture-and-link-an-analyst-note.mp4` | Record and link a TLS retest plan | Notes, Markdown, asset links |
| `07-review-and-approve-a-bounded-mission-action.mp4` | Review the exact service-detection request before approval | Missions, event replay, Activity, exact-request approval |
| `08-add-and-inspect-an-in-scope-asset.mp4` | Add the authorized API hostname to project scope | Assets, classification, tags, inspector |
| `09-preserve-immutable-evidence-with-provenance.mp4` | Store a TLS observation and review its recorded hash | Evidence upload, SHA-256, provenance, asset links |
| `10-ingest-and-inspect-a-cited-knowledge-source.mp4` | Add the rules of engagement for bounded retrieval | Sources, ingestion, citations, retrieval safety |
| `11-create-and-validate-an-evidence-backed-finding.mp4` | Record a TLS weakness, attach evidence, and validate it | Findings, identifiers, lifecycle, evidence links |
| `12-build-sign-off-and-export-a-report.mp4` | Build, sign, and export the assessment deliverable | Reports, findings/notes, revision save, sign-off, PDF |
| `13-configure-models-identity-and-appearance.mp4` | Configure local AI, analyst attribution, and confirm the Zero theme | Setup, Advanced settings, providers, identity, appearance |
| `14-inspect-a-correlated-diagnostic-failure.mp4` | Investigate a retained local-model stream failure | Diagnostics, error/request correlation, safe cause |

## Execution boundary

Every scenario starts a temporary, authenticated Nebula Core and uses its real
SQLite storage, artifact store, workspace, report renderer, and HTTP contracts.
The temporary Core data directory is removed after the suite.

Three external boundaries use deterministic adapters so recordings are safe and
repeatable on developer and CI machines:

- the Kali terminal transport uses a content-pinned, non-networked transcript;
- the local model returns a fixed cited answer through Nebula's streaming wire
  contract;
- the desktop Browser uses the Tauri command boundary without contacting the
  reserved `.test` host.

The mission approval scenario likewise replays a fixed Core-shaped run ledger
and exact request. All targets use reserved `.test` names, and the suite never
scans or contacts a live system.
