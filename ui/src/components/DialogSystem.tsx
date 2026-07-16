import {
  createContext,
  type KeyboardEvent as ReactKeyboardEvent,
  type PropsWithChildren,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { AlertTriangle, X } from "lucide-react";

const FOCUSABLE = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

interface ModalSurfaceProps extends PropsWithChildren {
  className?: string;
  labelledBy: string;
  onClose: () => void;
}

export function ModalSurface({ children, className = "", labelledBy, onClose }: ModalSurfaceProps) {
  const surfaceRef = useRef<HTMLDivElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const surface = surfaceRef.current;
    requestAnimationFrame(() => {
      const target = surface?.querySelector<HTMLElement>("[data-autofocus]")
        ?? surface?.querySelector<HTMLElement>(FOCUSABLE);
      target?.focus();
    });
    return () => returnFocusRef.current?.focus();
  }, []);

  const trapFocus = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const items = [...(surfaceRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
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

  return (
    <div className="dialog-backdrop refined-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        ref={surfaceRef}
        className={`modal-surface ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        onKeyDown={trapFocus}
        onMouseDown={(event) => event.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

export interface ConfirmationOptions {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
}

interface PendingConfirmation {
  options: ConfirmationOptions;
  resolve: (value: boolean) => void;
}

type Confirm = (options: ConfirmationOptions) => Promise<boolean>;
const ConfirmationContext = createContext<Confirm | undefined>(undefined);
const DialogOpenContext = createContext(false);

export function DialogProvider({ children }: PropsWithChildren) {
  const [pending, setPending] = useState<PendingConfirmation>();

  const confirm = useCallback<Confirm>((options) => new Promise((resolve) => {
    setPending({ options, resolve });
  }), []);

  const finish = (value: boolean) => {
    pending?.resolve(value);
    setPending(undefined);
  };

  return (
    <ConfirmationContext.Provider value={confirm}>
      <DialogOpenContext.Provider value={Boolean(pending)}>
        {children}
        {pending && (
        <ModalSurface labelledBy="confirmation-title" className="confirmation-dialog" onClose={() => finish(false)}>
          <header>
            <span className={`confirmation-icon ${pending.options.tone ?? "default"}`} aria-hidden="true">
              <AlertTriangle size={20} />
            </span>
            <div>
              <h2 id="confirmation-title">{pending.options.title}</h2>
              <div className="confirmation-message">{pending.options.message}</div>
            </div>
            <button className="icon-button subtle" type="button" aria-label="Close" onClick={() => finish(false)}>
              <X size={17} />
            </button>
          </header>
          <footer>
            <button className="button secondary" type="button" onClick={() => finish(false)}>
              {pending.options.cancelLabel ?? "Cancel"}
            </button>
            <button
              className={`button ${pending.options.tone === "danger" ? "danger" : "primary"}`}
              type="button"
              data-autofocus
              onClick={() => finish(true)}
            >
              {pending.options.confirmLabel ?? "Continue"}
            </button>
          </footer>
        </ModalSurface>
        )}
      </DialogOpenContext.Provider>
    </ConfirmationContext.Provider>
  );
}

export function useDialogOpen(): boolean {
  return useContext(DialogOpenContext);
}

export function useConfirmation(): Confirm {
  const context = useContext(ConfirmationContext);
  if (!context) throw new Error("useConfirmation must be used inside DialogProvider");
  return context;
}
