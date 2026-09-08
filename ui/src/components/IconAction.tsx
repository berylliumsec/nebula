import type { ButtonHTMLAttributes } from "react";
import type { LucideIcon } from "lucide-react";

/** Familiar secondary actions keep one accessible name and a quiet touch target. */
export function IconAction({ icon: Icon, label, className = "", title, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { icon: LucideIcon; label: string }) {
  return <button {...props} type={props.type ?? "button"} className={`icon-button subtle quiet-icon-action ${className}`} aria-label={label} title={title ?? label}><Icon size={18} aria-hidden="true" /></button>;
}
