import {
  ArrowUpRight,
  Bot,
  FileText,
  FolderKanban,
  Radar,
  Settings2,
  ShieldAlert,
  type LucideIcon,
} from "lucide-react";
import { Link } from "react-router-dom";
import type { ZeroModule, ZeroModuleIcon } from "./zeroLayerModules";

const icons: Record<ZeroModuleIcon, LucideIcon> = {
  approval: ShieldAlert,
  mission: Radar,
  finding: ShieldAlert,
  setup: Settings2,
  report: FileText,
  project: FolderKanban,
};

interface ZeroLayerDeckProps {
  modules: ZeroModule[];
  onOpenActivity: (view: "approvals" | "activity") => void;
}

function ModuleContents({ module }: { module: ZeroModule }) {
  const Icon = icons[module.icon] ?? Bot;
  return (
    <>
      <span className="zero-module-icon" aria-hidden="true"><Icon size={20} strokeWidth={1.65} /></span>
      <span className="zero-module-copy">
        <small>{module.eyebrow}</small>
        <strong>{module.title}</strong>
        <span>{module.detail}</span>
      </span>
      <span className="zero-module-action">{module.actionLabel}<ArrowUpRight size={14} aria-hidden="true" /></span>
    </>
  );
}

export function ZeroLayerDeck({ modules, onOpenActivity }: ZeroLayerDeckProps) {
  return (
    <section className="zero-layer-deck" aria-label="Zero Layer context">
      <header className="zero-layer-heading">
        <span aria-hidden="true" />
        <div><strong>Zero Layer</strong><small>Live workspace priorities</small></div>
      </header>
      <div className="zero-layer-modules">
        {modules.map((module) => {
          const action = module.action;
          return action.type === "route" ? (
            <Link className="zero-layer-module" data-tone={module.tone} to={action.to} key={module.id}>
              <ModuleContents module={module} />
            </Link>
          ) : (
            <button className="zero-layer-module" data-tone={module.tone} type="button" onClick={() => onOpenActivity(action.view)} key={module.id}>
              <ModuleContents module={module} />
            </button>
          );
        })}
      </div>
    </section>
  );
}
