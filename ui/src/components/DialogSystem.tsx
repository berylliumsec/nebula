import {
  createContext,
  type KeyboardEvent as ReactKeyboardEvent,
  type FormEventHandler,
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

let lastPointerTarget: HTMLElement | null = null;
if (typeof document !== "undefined") {
  const rememberDialogTrigger = (event: Event) => {
    if (event.target instanceof Element) lastPointerTarget = event.target.closest<HTMLElement>(FOCUSABLE);
  };
  document.addEventListener("pointerdown", rememberDialogTrigger, true);
  document.addEventListener("click", rememberDialogTrigger, true);
}

interface ModalSurfaceProps extends PropsWithChildren {
  className?: string;
  labelledBy: string;
  onClose: () => void;
  as?: "div" | "form" | "section";
  onSubmit?: FormEventHandler<HTMLFormElement>;
  noValidate?: boolean;
}

export function ModalSurface({ children, className = "", labelledBy, onClose, as = "div", onSubmit, noValidate }: ModalSurfaceProps) {
  const surfaceRef = useRef<HTMLElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const returnFocusIdentityRef = useRef<{ tag: string; ariaLabel: string; text: string } | undefined>(undefined);

  useEffect(() => {
    const surface = surfaceRef.current;
    const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const active = activeElement && activeElement !== document.body && !surface?.contains(activeElement)
      ? activeElement
      : lastPointerTarget;
    if (!returnFocusRef.current) {
      returnFocusRef.current = active;
      returnFocusIdentityRef.current = returnFocusRef.current ? {
        tag: returnFocusRef.current.tagName,
        ariaLabel: returnFocusRef.current.getAttribute("aria-label") ?? "",
        text: returnFocusRef.current.textContent?.replace(/\s+/g, " ").trim() ?? "",
      } : undefined;
    }
    requestAnimationFrame(() => {
      const target = surface?.querySelector<HTMLElement>("[data-autofocus], [autofocus]")
        ?? surface?.querySelector<HTMLElement>(FOCUSABLE);
      target?.focus();
    });
    return () => {
      const previous = returnFocusRef.current;
      const identity = returnFocusIdentityRef.current;
      const restoreFocus = () => {
        if (previous?.isConnected) {
          previous.focus();
          return;
        }
        if (!identity) return;
        const replacement = [...document.querySelectorAll<HTMLElement>(identity.tag.toLowerCase())].find((element) => (
          (identity.ariaLabel && element.getAttribute("aria-label") === identity.ariaLabel)
          || (identity.text && element.textContent?.replace(/\s+/g, " ").trim() === identity.text)
        ));
        replacement?.focus();
      };
      restoreFocus();
      queueMicrotask(() => {
        if (document.activeElement === document.body) restoreFocus();
      });
    };
  }, []);

  useEffect(() => {
    const closeFromDocument = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const dialogs = [...document.querySelectorAll<HTMLElement>(".dialog-backdrop > .modal-surface")];
      if (dialogs.at(-1) !== surfaceRef.current) return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", closeFromDocument, true);
    return () => document.removeEventListener("keydown", closeFromDocument, true);
  }, [onClose]);

  const trapFocus = (event: ReactKeyboardEvent<HTMLElement>) => {
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

  const Surface = as;
  return (
    <div className="dialog-backdrop refined-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <Surface
        ref={surfaceRef as never}
        className={`modal-surface ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        onSubmit={as === "form" ? onSubmit as never : undefined}
        noValidate={as === "form" ? noValidate : undefined}
        onKeyDown={trapFocus as never}
        onMouseDown={(event) => event.stopPropagation()}
      >
        {children}
      </Surface>
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
          <header role="presentation">
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
