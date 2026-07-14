import { BookOpen, FileSearch, LayoutDashboard, Network } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { AssetsPage } from "./AssetsPage";
import { EvidencePage } from "./EvidencePage";
import { KnowledgePage } from "./KnowledgePage";
import { OverviewPage } from "./OverviewPage";

type ProjectView = "overview" | "assets" | "evidence" | "sources";

const projectViews = [
  { id: "overview" as const, label: "Overview", icon: LayoutDashboard },
  { id: "assets" as const, label: "Assets", icon: Network },
  { id: "evidence" as const, label: "Evidence", icon: FileSearch },
  { id: "sources" as const, label: "Sources", icon: BookOpen },
];

function isProjectView(value: string | null): value is ProjectView {
  return projectViews.some((item) => item.id === value);
}

export function ProjectPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("view");
  const view: ProjectView = isProjectView(requested) ? requested : "overview";

  const selectView = (next: ProjectView) => {
    const params = new URLSearchParams(searchParams);
    params.set("view", next);
    if (next !== "sources") params.delete("source");
    if (next !== "evidence") params.delete("id");
    setSearchParams(params, { replace: true });
  };

  return (
    <div className="project-workspace">
      <nav className="project-tabs" aria-label="Project sections">
        {projectViews.map(({ id, label, icon: Icon }) => (
          <button
            className={view === id ? "active" : undefined}
            type="button"
            aria-current={view === id ? "page" : undefined}
            onClick={() => selectView(id)}
            key={id}
          >
            <Icon size={16} aria-hidden="true" />
            {label}
          </button>
        ))}
      </nav>
      {view === "overview" ? <OverviewPage /> : null}
      {view === "assets" ? <AssetsPage /> : null}
      {view === "evidence" ? <EvidencePage /> : null}
      {view === "sources" ? <KnowledgePage /> : null}
    </div>
  );
}
