import { useEffect, useRef, useState } from "react";
import { CheckCircle2 } from "lucide-react";

const SETTINGS_SAVED_EVENT = "nebula:settings-saved";

export function announceSettingsSaved(message: string) {
  window.dispatchEvent(new CustomEvent<string>(SETTINGS_SAVED_EVENT, { detail: message }));
}

export function SettingsSaveFeedback() {
  const [message, setMessage] = useState<string>();
  const timeout = useRef<number | undefined>(undefined);

  useEffect(() => {
    const show = (event: Event) => {
      window.clearTimeout(timeout.current);
      setMessage((event as CustomEvent<string>).detail);
      timeout.current = window.setTimeout(() => setMessage(undefined), 2800);
    };
    window.addEventListener(SETTINGS_SAVED_EVENT, show);
    return () => {
      window.removeEventListener(SETTINGS_SAVED_EVENT, show);
      window.clearTimeout(timeout.current);
    };
  }, []);

  return message ? (
    <div className="settings-save-feedback" role="status" aria-live="polite">
      <span><CheckCircle2 size={17} aria-hidden="true" /></span>
      <div><strong>Saved</strong><small>{message}</small></div>
    </div>
  ) : null;
}
