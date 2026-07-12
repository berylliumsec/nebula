import { CheckCircle2, Download, Eye, FileText, MoreHorizontal, Plus, ShieldCheck } from "lucide-react";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";

export function ReportsPage() {
  const { previewMode } = useWorkspace();
  return (
    <div className="page reports-page">
      <PageHeader eyebrow="Defensible deliverables" title="Reports" description="Compose executive and technical narratives from verified findings and cited evidence." actions={<button className="button primary" type="button"><Plus size={16} /> New report</button>} />
      {!previewMode ? (
        <section className="panel empty-state"><FileText size={28} /><strong>No report selected</strong><p>Connect the Core reports resource or create a report to open the composer.</p></section>
      ) : <div className="report-layout">
        <aside className="panel report-outline">
          <header><div><span>Draft report</span><strong>Acme external assessment</strong></div><button className="icon-button subtle" type="button" aria-label="Report actions"><MoreHorizontal size={17} /></button></header>
          <nav aria-label="Report outline"><button className="complete" type="button"><CheckCircle2 size={15} /> Cover & metadata</button><button className="active" type="button"><FileText size={15} /> Executive summary</button><button type="button"><span>3</span> Scope & methodology</button><button type="button"><span>4</span> Risk overview</button><button type="button"><span>5</span> Technical findings <em>12</em></button><button type="button"><span>6</span> Remediation roadmap</button><button type="button"><span>7</span> Appendices</button></nav>
          <footer><span>Completion</span><strong>68%</strong><div className="progress-track small"><span style={{ width: "68%" }} /></div></footer>
        </aside>
        <section className="panel report-editor">
          <header className="report-editor-toolbar"><div><button type="button"><strong>B</strong></button><button type="button"><em>I</em></button><button type="button">H2</button><span /><button type="button">• List</button><button type="button">Add citation</button></div><span>Saved locally</span></header>
          <article className="report-document" contentEditable suppressContentEditableWarning aria-label="Executive summary editor">
            <span className="document-kicker">Executive summary</span>
            <h1>Acme external security assessment</h1>
            <p>Nebula Security assessed Acme’s externally accessible systems and supporting APIs between July 8 and July 12, 2026. Testing followed the customer-approved rules of engagement and remained within the documented asset and time boundaries.</p>
            <h2>Overall posture</h2>
            <p>The assessment identified one critical and four high-severity weaknesses requiring prioritized remediation. The most urgent issue affects an internet-facing gateway and has evidence of active exploitation in the wild.</p>
            <blockquote contentEditable={false}><ShieldCheck size={17} /><span><strong>Evidence-backed statement</strong> · 3 findings · 8 immutable artifacts</span><button type="button">Inspect citations</button></blockquote>
            <h2>Recommended next actions</h2>
            <ol><li>Apply the vendor update to the affected gateway within 24 hours.</li><li>Restrict administrative access and require phishing-resistant MFA.</li><li>Schedule a targeted retest after the remediation window.</li></ol>
          </article>
        </section>
        <aside className="panel report-review">
          <header><h2>Review & export</h2><p>Checks update from structured report data.</p></header>
          <ul><li className="pass"><CheckCircle2 size={16} /><span><strong>Evidence coverage</strong><small>12 of 12 verified findings cited</small></span></li><li className="pass"><CheckCircle2 size={16} /><span><strong>Required sections</strong><small>All consultancy sections present</small></span></li><li className="warning"><Eye size={16} /><span><strong>Reviewer sign-off</strong><small>Waiting for Morgan Lee</small></span></li></ul>
          <div className="export-actions"><button className="button primary full" type="button"><Eye size={15} /> Preview report</button><button className="button secondary full" type="button"><Download size={15} /> Export…</button></div>
          <p className="export-formats">PDF · HTML · Markdown · JSON · SARIF</p>
        </aside>
      </div>}
    </div>
  );
}
