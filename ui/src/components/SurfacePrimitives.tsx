import { useEffect, useRef, type HTMLAttributes, type KeyboardEvent, type ReactNode } from "react";
import { AlertCircle, AlertTriangle, CircleAlert, Info, LoaderCircle, X } from "lucide-react";

export type StatusTone = "neutral" | "informational" | "success" | "warning" | "danger";

interface StatusChipProps {
  children: ReactNode;
  tone?: StatusTone;
  className?: string;
}

/** Compact status language shared by tables, cards, inspectors, and toolbars. */
export function StatusChip({ children, tone = "neutral", className = "" }: StatusChipProps) {
  return <span className={`status-chip tone-${tone} ${className}`.trim()}>{children}</span>;
}

interface SectionHeaderProps {
  id?: string;
  title: ReactNode;
  description?: ReactNode;
  eyebrow?: ReactNode;
  actions?: ReactNode;
  className?: string;
}

/** One title/description/action anatomy for sections below the page heading. */
export function SectionHeader({ id, title, description, eyebrow, actions, className = "" }: SectionHeaderProps) {
  return (
    <header className={`section-heading ${className}`.trim()}>
      <div>{eyebrow && <span className="section-kicker">{eyebrow}</span>}<h2 id={id}>{title}</h2>{description && <p>{description}</p>}</div>
      {actions && <div className="section-actions">{actions}</div>}
    </header>
  );
}

interface SurfacePanelProps {
  children: ReactNode;
  className?: string;
  labelledBy?: string;
}

/** The default bounded content surface. Avoid nesting it for decorative depth. */
export function SurfacePanel({ children, className = "", labelledBy }: SurfacePanelProps) {
  return <section className={`panel surface-panel ${className}`.trim()} aria-labelledby={labelledBy}>{children}</section>;
}

export type SurfaceVariant = "plain" | "grouped" | "overlay";

interface SurfaceProps extends HTMLAttributes<HTMLElement> {
  children: ReactNode;
  variant?: SurfaceVariant;
  labelledBy?: string;
  as?: "section" | "article" | "div";
}

/** Authoritative containment primitive. Plain surfaces add no decorative frame. */
export function Surface({ children, variant = "plain", labelledBy, as = "section", className = "", ...props }: SurfaceProps) {
  const Element = as;
  return <Element className={`surface surface-${variant} ${className}`.trim()} aria-labelledby={labelledBy} {...props}>{children}</Element>;
}

interface ToolbarProps extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode;
  label: string;
  primaryAction?: ReactNode;
  overflowAction?: ReactNode;
}

/** Flat toolbar with a single explicit primary decision and optional overflow. */
export function Toolbar({ children, label, primaryAction, overflowAction, className = "", ...props }: ToolbarProps) {
  return <div className={`toolbar ${className}`.trim()} role="toolbar" aria-label={label} {...props}><div className="toolbar-content">{children}</div>{(primaryAction || overflowAction) && <div className="toolbar-actions">{overflowAction}{primaryAction}</div>}</div>;
}

export interface TabItem<T extends string> {
  id: T;
  label: ReactNode;
  icon?: ReactNode;
  ariaLabel?: string;
  disabled?: boolean;
}

interface TabBarProps<T extends string> {
  items: readonly TabItem<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  className?: string;
}

/** Quiet, keyboard-navigable tab anatomy shared by every tool switcher. */
export function TabBar<T extends string>({ items, value, onChange, label, className = "" }: TabBarProps<T>) {
  const tabListRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const selected = tabListRef.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]');
    if (typeof selected?.scrollIntoView === "function") selected.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [value]);
  const moveFocus = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...event.currentTarget.parentElement!.querySelectorAll<HTMLButtonElement>('[role="tab"]:not(:disabled)')];
    const current = tabs.indexOf(event.currentTarget);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : event.key === "ArrowRight" ? (current + 1) % tabs.length : (current - 1 + tabs.length) % tabs.length;
    tabs[next]?.focus();
    tabs[next]?.click();
  };
  return <div ref={tabListRef} className={`tab-bar ${className}`.trim()} role="tablist" aria-label={label}>{items.map((item) => <button key={item.id} type="button" role="tab" aria-label={item.ariaLabel} aria-selected={value === item.id} tabIndex={value === item.id ? 0 : -1} disabled={item.disabled} onKeyDown={moveFocus} onClick={() => onChange(item.id)}>{item.icon}{item.label}</button>)}</div>;
}

interface ListRowProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  title: ReactNode;
  metadata?: ReactNode;
  status?: ReactNode;
  actions?: ReactNode;
  leading?: ReactNode;
}

/** Consistent row hierarchy with quiet actions that remain reachable by focus and touch. */
export function ListRow({ title, metadata, status, actions, leading, className = "", ...props }: ListRowProps) {
  return <article className={`list-row ${className}`.trim()} {...props}>{leading && <span className="list-row-leading">{leading}</span>}<div className="list-row-copy"><strong>{title}</strong>{metadata && <small>{metadata}</small>}</div>{status && <div className="list-row-status">{status}</div>}{actions && <div className="list-row-actions">{actions}</div>}</article>;
}

export type NoticeSeverity = "informational" | "warning" | "actionable-error" | "blocking-error";

interface SurfaceNoticeProps {
  severity?: NoticeSeverity;
  title: string;
  detail?: ReactNode;
  actions?: ReactNode;
  dismissLabel?: string;
  onDismiss?: () => void;
  className?: string;
}

const noticeIcons = {
  informational: Info,
  warning: AlertTriangle,
  "actionable-error": AlertCircle,
  "blocking-error": CircleAlert,
} as const;

/** Shared notice anatomy. Only blocking errors interrupt assistive technology. */
export function SurfaceNotice({
  severity = "informational",
  title,
  detail,
  actions,
  dismissLabel = "Dismiss notice",
  onDismiss,
  className = "",
}: SurfaceNoticeProps) {
  const Icon = noticeIcons[severity];
  return (
    <div
      className={`surface-notice tone-${severity} ${className}`.trim()}
      role={severity === "blocking-error" ? "alert" : "status"}
    >
      <Icon size={17} aria-hidden="true" />
      <div className="surface-notice-copy"><strong>{title}</strong>{detail && <span>{detail}</span>}</div>
      {(actions || onDismiss) && <div className="surface-notice-actions">
        {actions}
        {onDismiss && <button className="icon-button subtle" type="button" aria-label={dismissLabel} onClick={onDismiss}><X size={15} aria-hidden="true" /></button>}
      </div>}
    </div>
  );
}

interface StandardEmptyStateProps {
  icon?: ReactNode;
  title: string;
  explanation: ReactNode;
  primaryAction?: ReactNode;
  secondaryAction?: ReactNode;
  compact?: boolean;
  className?: string;
}

/** Shared empty-state contract: one explanation and at most one primary decision. */
export function StandardEmptyState({
  icon,
  title,
  explanation,
  primaryAction,
  secondaryAction,
  compact = false,
  className = "",
}: StandardEmptyStateProps) {
  return (
    <section className={`empty-state standard-empty-state${compact ? " compact" : ""} ${className}`.trim()}>
      {icon}
      <strong>{title}</strong>
      <p>{explanation}</p>
      {(primaryAction || secondaryAction) && <div className="empty-state-actions">{primaryAction}{secondaryAction}</div>}
    </section>
  );
}

export type SurfaceStateKind = "loading" | "empty" | "unsupported" | "failure";

interface SurfaceStateProps extends Omit<StandardEmptyStateProps, "compact"> {
  kind: SurfaceStateKind;
  compact?: boolean;
}

/** Loading, empty, unsupported, and failure states retain the same geometry. */
export function SurfaceState({ kind, icon, className = "", ...props }: SurfaceStateProps) {
  const fallbackIcon = kind === "loading" ? <LoaderCircle className="spin" size={21} aria-hidden="true" /> : undefined;
  return <StandardEmptyState icon={icon ?? fallbackIcon} className={`surface-state state-${kind} ${className}`.trim()} {...props} />;
}

interface ProgressStateProps {
  stage: string;
  detail: ReactNode;
  progressLabel?: string;
  elapsed?: string;
  progress?: number;
  currentAction?: ReactNode;
  cancelAction?: ReactNode;
  retryAction?: ReactNode;
  className?: string;
}

/** Truthful determinate or indeterminate progress with valid recovery actions. */
export function ProgressState({ stage, detail, progressLabel, elapsed, progress, currentAction, cancelAction, retryAction, className = "" }: ProgressStateProps) {
  const determinate = typeof progress === "number" && Number.isFinite(progress);
  const value = determinate ? Math.max(0, Math.min(100, progress)) : undefined;
  return <section className={`progress-state${determinate ? "" : " indeterminate"} ${className}`.trim()} aria-label={stage} aria-busy={!retryAction}><header><div><strong>{stage}</strong><span>{detail}</span></div>{elapsed && <small>{elapsed}</small>}</header><div className="progress-state-track" role="progressbar" aria-label={progressLabel ?? `${stage} progress`} aria-valuetext={typeof detail === "string" ? detail : undefined} aria-valuemin={determinate ? 0 : undefined} aria-valuemax={determinate ? 100 : undefined} aria-valuenow={value}><span style={determinate ? { width: `${value}%` } : undefined} /></div>{(currentAction || cancelAction || retryAction) && <footer>{currentAction && <span>{currentAction}</span>}<div>{cancelAction}{retryAction}</div></footer>}</section>;
}

interface SettingsGroupProps {
  id: string;
  title: string;
  summary: ReactNode;
  open: boolean;
  onOpen: (id: string) => void;
  children: ReactNode;
  bodyClassName?: string;
  hidden?: boolean;
}

/** A URL-controlled Settings disclosure. Opening one group closes its sibling. */
export function SettingsGroup({ id, title, summary, open, onOpen, children, bodyClassName = "", hidden = false }: SettingsGroupProps) {
  return (
    <details
      className="settings-group"
      id={id}
      open={open}
      hidden={hidden}
    >
      <summary onClick={(event) => { event.preventDefault(); onOpen(id); }}><span><strong>{title}</strong><small>{summary}</small></span></summary>
      <div className={`settings-group-body ${bodyClassName}`.trim()}>{children}</div>
    </details>
  );
}
