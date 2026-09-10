import type { ButtonHTMLAttributes, ReactNode } from "react";
import { createPortal } from "react-dom";
import { useChrome } from "../state/ChromeContext";

/** One contract for full desktop labels and named icon-only mobile actions. */
export function PageHeaderAction({ label, icon, className = "button primary", title, ...props }: Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> & {label: string; icon: ReactNode}) {
  return <button {...props} type={props.type ?? "button"} className={`${className} page-header-action`} aria-label={label} title={title ?? label}><span className="page-header-action-icon" aria-hidden="true">{icon}</span><span className="page-header-action-label">{label}</span></button>;
}

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
