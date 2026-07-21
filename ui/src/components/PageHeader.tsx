import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { useChrome } from "../state/ChromeContext";

interface PageHeaderProps {
  eyebrow?: string;
  title: string;
  description: string;
  actions?: ReactNode;
  showIntroduction?: boolean;
}

export function PageHeader({ eyebrow, title, description, actions, showIntroduction = true }: PageHeaderProps) {
  const { toolbarHost } = useChrome();
  return (
    <>
    {showIntroduction && <header className="page-header">
      <div>
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {!toolbarHost && actions && <div className="page-actions">{actions}</div>}
    </header>}
    {actions && toolbarHost && createPortal(<div className="page-actions toolbar-page-actions">{actions}</div>, toolbarHost)}
    </>
  );
}
