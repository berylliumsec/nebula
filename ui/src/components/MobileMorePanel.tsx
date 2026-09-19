import { useEffect, useRef, type ReactNode } from "react";
import {
  BookMarked,
  BookOpen,
  Bot,
  Bug,
  Braces,
  ChevronRight,
  FileText,
  FolderKanban,
  FolderOpen,
  Globe2,
  Maximize2,
  NotebookPen,
  Search,
  Settings,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useOptionalGuides } from "../guides/GuideProvider";
import { projectRoot, projectSurface } from "../resourceRoutes";
import { useOptionalChrome } from "../state/ChromeContext";
import { useWorkspace } from "../state/WorkspaceContext";

export type MobileMoreView = "workspace" | "code" | "notes" | "missions" | "browser";

const tools: ReadonlyArray<readonly [MobileMoreView, string, typeof FolderOpen]> = [
  ["workspace", "Files", FolderOpen],
  ["code", "Code", Braces],
  ["notes", "Notes", NotebookPen],
  ["missions", "Missions", Bot],
  ["browser", "Browser", Globe2],
];

function Row({ icon, label, detail, onClick }: { icon: ReactNode; label: string; detail?: string; onClick: () => void }) {
  return <button type="button" className="mobile-more-row" onClick={onClick}>
    <span className="mobile-more-row-icon" aria-hidden="true">{icon}</span>
    <span className="mobile-more-row-label">{label}</span>
    {detail && <span className="mobile-more-row-detail">{detail}</span>}
    <ChevronRight size={16} aria-hidden="true" />
  </button>;
}

/** Phone "More" tab: project, workspace tools, project pages, then Settings. */
export function MobileMorePanel({ view, onSelectView, onFocusMode, onClose }: {
  view: string;
  onSelectView: (view: MobileMoreView) => void;
  onFocusMode: () => void;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const chrome = useOptionalChrome();
  const guides = useOptionalGuides();
  const { engagement } = useWorkspace();
  const titleRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => { titleRef.current?.focus({ preventScroll: true }); }, []);
  const go = (path: string) => { onClose(); navigate(path); };
  const initial = (engagement?.name.trim()[0] ?? "N").toUpperCase();
  return <section className="mobile-more-panel" id="mobile-workbench-more" role="dialog" aria-modal="true" aria-labelledby="mobile-more-title">
    <header className="mobile-more-title">
      <h1 id="mobile-more-title" ref={titleRef} tabIndex={-1}>More</h1>
      {chrome && <button className="icon-button subtle mobile-more-search" type="button" aria-label="Search pages, actions, and settings" onClick={() => { onClose(); chrome.openPalette(); }}><Search size={19} aria-hidden="true" /></button>}
    </header>
    {engagement && <div className="mobile-more-project">
      <span className="mobile-project-avatar" aria-hidden="true">{initial}</span>
      <span className="mobile-more-project-copy"><strong>{engagement.name}</strong><small>{engagement.workspacePath ?? "Nebula workspace"}</small></span>
      {chrome?.openProjectPicker && <button className="button quiet" type="button" aria-label="Switch project" onClick={() => { onClose(); chrome.openProjectPicker?.(); }}>Switch</button>}
    </div>}
    <div className="mobile-more-tools" role="group" aria-label="Workspace tools">
      {tools.map(([id, label, Icon]) => <button type="button" key={id} aria-current={view === id ? "page" : undefined} onClick={() => { onSelectView(id); onClose(); }}>
        <Icon size={22} aria-hidden="true" /><span>{label}</span>
      </button>)}
      {guides && <button type="button" onClick={() => { onClose(); guides.openHub(); }}><BookOpen size={22} aria-hidden="true" /><span>Guides</span></button>}
    </div>
    {engagement && <nav className="mobile-more-group" aria-label="Project pages">
      <Row icon={<Bug size={19} />} label="Findings" onClick={() => go(projectSurface(engagement.id, "findings"))} />
      <Row icon={<FileText size={19} />} label="Reports" onClick={() => go(projectSurface(engagement.id, "reports"))} />
      <Row icon={<FolderKanban size={19} />} label="Project overview" onClick={() => go(projectRoot(engagement.id))} />
      <Row icon={<BookMarked size={19} />} label="Library" onClick={() => go("/library")} />
    </nav>}
    <nav className="mobile-more-group" aria-label="App">
      <Row icon={<Settings size={19} />} label="Settings" onClick={() => go("/settings")} />
      <Row icon={<Maximize2 size={19} />} label="Focus mode" onClick={() => { onClose(); onFocusMode(); }} />
    </nav>
  </section>;
}
