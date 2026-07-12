import { Bug, CheckCircle2, Filter, GitCompareArrows, Search, ShieldAlert } from "lucide-react";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";

export function FindingsPage() {
  const { findings } = useWorkspace();
  const attention = findings.filter((finding) => ["critical", "high"].includes(finding.severity)).length;
  const critical = findings.filter((finding) => finding.severity === "critical").length;
  const awaitingVerification = findings.filter((finding) => ["candidate", "validated"].includes(finding.status)).length;
  const candidates = findings.filter((finding) => finding.status === "candidate").length;
  const remediated = findings.filter((finding) => ["remediated", "retest_passed"].includes(finding.status)).length;
  const retested = findings.filter((finding) => finding.status.startsWith("retest_")).length;
  const advisoryLinked = findings.filter((finding) => finding.cveIds.length > 0).length;
  return (
    <div className="page findings-page">
      <PageHeader
        eyebrow="Evidence-backed risk"
        title="Findings"
        description="Track each candidate through validation, independent verification, remediation, and retest."
        actions={<button className="button secondary" type="button"><GitCompareArrows size={16} /> Compare scan</button>}
      />
      <section className="finding-summary-grid" aria-label="Finding lifecycle summary">
        <article><span className="summary-icon red"><ShieldAlert size={18} /></span><div><strong>{attention}</strong><small>High or critical</small></div><em>{critical} critical</em></article>
        <article><span className="summary-icon violet"><Bug size={18} /></span><div><strong>{awaitingVerification}</strong><small>Awaiting verification</small></div><em>{candidates} candidates</em></article>
        <article><span className="summary-icon green"><CheckCircle2 size={18} /></span><div><strong>{remediated}</strong><small>Remediated</small></div><em>{retested} retested</em></article>
        <article><div className="epss-gauge"><span style={{ width: findings.length ? `${(advisoryLinked / findings.length) * 100}%` : "0%" }} /></div><div><strong>{advisoryLinked}</strong><small>Advisory linked</small></div><em>CVE identifiers</em></article>
      </section>
      <section className="panel data-panel">
        <header className="data-toolbar">
          <label className="search-field"><Search size={16} /><span className="sr-only">Search findings</span><input type="search" placeholder="Search title, CVE, CWE, asset…" /></label>
          <div><button className="button quiet" type="button"><Filter size={15} /> Severity</button><button className="button quiet" type="button">Status</button><button className="button quiet" type="button">Assignee</button></div>
        </header>
        <div className="table-scroll">
          <table className="data-table findings-table">
            <thead><tr><th scope="col">Severity</th><th scope="col">Finding</th><th scope="col">Status</th><th scope="col">Assets</th><th scope="col">Evidence</th><th scope="col">Updated</th></tr></thead>
            <tbody>
              {findings.map((finding) => (
                <tr key={finding.id}>
                  <td><span className={`severity-label ${finding.severity}`}><span />{finding.severity}</span></td>
                  <td><div className="finding-title"><strong>{finding.title}</strong><small>{finding.cveIds.length ? finding.cveIds.join(", ") : "No advisory identifier"}</small></div></td>
                  <td><span className={`lifecycle-badge ${finding.status}`}>{finding.status.replace("_", " ")}</span></td>
                  <td>{finding.affectedAssetCount}</td>
                  <td>{finding.evidenceCount}</td>
                  <td>{new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(finding.updatedAt))}</td>
                </tr>
              ))}
              {findings.length === 0 && <tr><td colSpan={6}>No findings have been recorded for this engagement.</td></tr>}
            </tbody>
          </table>
        </div>
        <footer className="table-footer"><span>Showing {findings.length} loaded findings</span><span>Correlation state is supplied by Core</span></footer>
      </section>
    </div>
  );
}
