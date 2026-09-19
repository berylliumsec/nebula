import { Compass } from "lucide-react";
import { useOptionalGuides } from "./GuideProvider";

/** Beside a feature configured outside the UI: start (or resume) its guide in place. */
export function ShowMeHow({ guide, step, label = "Show me how" }: { guide: string; step?: number; label?: string }) {
  const guides = useOptionalGuides();
  if (!guides) return null;
  return <button className="button quiet guide-show-me" type="button" onClick={() => guides.start(guide, step)}>
    <Compass size={14} aria-hidden="true" /> {label}
  </button>;
}
