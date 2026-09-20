import { lazy, Suspense, useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { ExternalLink, SlidersHorizontal, X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import type { SettingCatalogEntry } from "../settingsCatalog";

const SettingsPage = lazy(() => import("../pages/SettingsPage").then((module) => ({ default: module.SettingsPage })));

interface SettingsLensProps {
  entry: SettingCatalogEntry;
  onClose: () => void;
  returnFocus?: HTMLElement | null;
}

const FOCUSABLE = [
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[href]",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function SettingsLens({ entry, onClose, returnFocus }: SettingsLensProps) {
  const navigate = useNavigate();
  const lensRef = useRef<HTMLDivElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    returnFocusRef.current = returnFocus ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    return () => returnFocusRef.current?.focus();
  }, [returnFocus]);

  // Focus moves into the lens as it opens; otherwise the Tab trap below is inert
  // and the page behind keeps keyboard focus until the operator clicks inside.
  useEffect(() => {
    lensRef.current?.focus({ preventScroll: true });
  }, []);

  // Escape is handled at document level like ModalSurface, so it works before
  // focus lands inside. A modal dialog opened above the lens owns Escape instead.
  useEffect(() => {
    const closeFromDocument = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (document.querySelector(".dialog-backdrop > .modal-surface")) return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", closeFromDocument, true);
    return () => document.removeEventListener("keydown", closeFromDocument, true);
  }, [onClose]);

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const items = [...(lensRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])]
      .filter((element) => element.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const openFullSettings = () => {
    onClose();
    navigate(`/settings#${entry.target}`);
  };

  return (
    <div className="settings-lens-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        ref={lensRef}
        className="settings-lens"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-lens-title"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="settings-lens-header">
          <span className="settings-lens-icon"><SlidersHorizontal size={18} aria-hidden="true" /></span>
          <div>
            <small>{entry.category} · {entry.scope}</small>
            <h2 id="settings-lens-title">{entry.label}</h2>
            <p>{entry.description}</p>
          </div>
          <button className="icon-button subtle" type="button" aria-label="Close setting" onClick={onClose}><X size={18} /></button>
        </div>
        <div className="settings-lens-content">
          <Suspense fallback={<div className="settings-lens-loading" role="status">Loading setting…</div>}>
            <SettingsPage embeddedTarget={entry.target} />
          </Suspense>
        </div>
        <footer>
          <span>Changes use the same validation and storage as Settings.</span>
          <button className="button secondary" type="button" onClick={openFullSettings}><ExternalLink size={14} /> Open full settings</button>
          <button className="button primary" type="button" onClick={onClose}>Done</button>
        </footer>
      </div>
    </div>
  );
}
