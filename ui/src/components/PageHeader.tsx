import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { useChrome } from "../state/ChromeContext";

interface PageHeaderProps {
  eyebrow?: string;
  title: string;
  description: string;
  actions?: ReactNode;
}

export function PageHeader({ eyebrow, title, description, actions }: PageHeaderProps) {
  const { toolbarHost } = useChrome();
  return (
    <header className="page-header">
      <div>
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions && (toolbarHost
        ? createPortal(<div className="page-actions toolbar-page-actions">{actions}</div>, toolbarHost)
        : <div className="page-actions">{actions}</div>)}
    </header>
  );
}
