import {
  createContext,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Clipboard, MessageSquareText, NotebookPen, Play } from "lucide-react";
import styles from "./SelectionActionsProvider.module.css";
import {
  copySelectionText,
  createSelectionDraft,
  isSelectableTextControl,
  readDomSelection,
  readTextControlSelection,
  SELECTION_TEXT_LIMIT,
  type DomSelectionOptions,
  type PresentSelectionInput,
  type SelectionActionDraft,
} from "./selectionActions";
import { logCaughtDiagnostic } from "../../diagnostics";

interface SelectionActionsContextValue {
  presentSelection(selection: PresentSelectionInput): void;
  dismissSelection(): void;
}

const SelectionActionsContext = createContext<SelectionActionsContextValue | undefined>(undefined);

export interface SelectionActionsProviderProps extends DomSelectionOptions {
  children: ReactNode;
  /** Opens an editable assistant draft. The provider never submits a turn. */
  onAsk: (draft: SelectionActionDraft) => void;
  /** Captures selected text as a project note. Omit to hide Take note. */
  onAddNote?: (draft: SelectionActionDraft) => void;
  /** Opens mandatory reviewed execution for explicitly runnable selections. */
  onRun?: (draft: SelectionActionDraft) => void;
  onCopyError?: (error: Error) => void;
}

export function useSelectionActions(): SelectionActionsContextValue {
  const value = useContext(SelectionActionsContext);
  if (!value) throw new Error("useSelectionActions must be used inside SelectionActionsProvider.");
  return value;
}

/** Lets optional surfaces, including isolated component tests, bind when a provider is present. */
export function useOptionalSelectionActions(): SelectionActionsContextValue | undefined {
  return useContext(SelectionActionsContext);
}

export function SelectionActionsProvider({
  children,
  onAsk,
  onAddNote,
  onRun,
  onCopyError,
  limit = SELECTION_TEXT_LIMIT,
  isSensitive,
  resolveSource,
}: SelectionActionsProviderProps) {
  const [draft, setDraft] = useState<SelectionActionDraft>();
  const [isExiting, setIsExiting] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);
  const exitTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const domOptions = useMemo(() => ({ limit, isSensitive, resolveSource }), [limit, isSensitive, resolveSource]);

  const presentSelection = useCallback((selection: PresentSelectionInput) => {
    if (exitTimerRef.current) clearTimeout(exitTimerRef.current);
    setIsExiting(false);
    setDraft(createSelectionDraft(selection, limit));
  }, [limit]);
  const dismissSelection = useCallback(() => {
    if (exitTimerRef.current) clearTimeout(exitTimerRef.current);
    exitTimerRef.current = undefined;
    setIsExiting(false);
    setDraft(undefined);
  }, []);
  const dismissSelectionElegantly = useCallback(() => {
    if (isExiting) return;
    setIsExiting(true);
    // animationend handles normal browsers; the timer also covers interrupted
    // animations and DOM environments that do not dispatch animation events.
    exitTimerRef.current = setTimeout(dismissSelection, 180);
  }, [dismissSelection, isExiting]);

  useEffect(() => () => {
    if (exitTimerRef.current) clearTimeout(exitTimerRef.current);
  }, []);

  useEffect(() => {
    const read = (event: Event) => {
      if (event instanceof KeyboardEvent && event.key === "Escape") return;
      if (popoverRef.current?.contains(event.target as Node | null)) return;
      const next = isSelectableTextControl(event.target)
        ? readTextControlSelection(event.target, domOptions)
        : readDomSelection(document.getSelection(), domOptions);
      setDraft(next);
    };
    const dismissForPointer = (event: PointerEvent) => {
      if (!popoverRef.current?.contains(event.target as Node | null)) setDraft(undefined);
    };
    const dismissForEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismissSelection();
    };
    document.addEventListener("pointerdown", dismissForPointer, true);
    document.addEventListener("pointerup", read, true);
    document.addEventListener("keyup", read, true);
    document.addEventListener("select", read, true);
    document.addEventListener("keydown", dismissForEscape, true);
    globalThis.addEventListener("resize", dismissSelection);
    return () => {
      document.removeEventListener("pointerdown", dismissForPointer, true);
      document.removeEventListener("pointerup", read, true);
      document.removeEventListener("keyup", read, true);
      document.removeEventListener("select", read, true);
      document.removeEventListener("keydown", dismissForEscape, true);
      globalThis.removeEventListener("resize", dismissSelection);
    };
  }, [dismissSelection, domOptions]);

  const context = useMemo(() => ({ presentSelection, dismissSelection }), [presentSelection, dismissSelection]);
  const viewportWidth = globalThis.innerWidth || 1024;
  const viewportHeight = globalThis.innerHeight || 768;
  const center = draft ? (draft.anchor.left + draft.anchor.right) / 2 : 0;
  const showAbove = draft ? draft.anchor.bottom + 64 > viewportHeight : false;
  const position = draft ? {
    left: Math.max(12, Math.min(center || 120, viewportWidth - 12)),
    top: showAbove ? Math.max(12, draft.anchor.top - 8) : Math.max(12, draft.anchor.bottom + 8),
  } : undefined;

  const preserveSelection = (event: ReactPointerEvent) => event.preventDefault();

  return <SelectionActionsContext.Provider value={context}>
    {children}
    {draft && <div
      ref={popoverRef}
      className={`${styles.popover}${isExiting ? ` ${styles.exiting}` : ""}`}
      style={position}
      role="toolbar"
      aria-label="Selected text actions"
      data-placement={showAbove ? "above" : "below"}
      onPointerDown={preserveSelection}
      onAnimationEnd={(event) => {
        if (isExiting && event.target === event.currentTarget) dismissSelection();
      }}
    >
      {draft.truncated && <span className={styles.notice} title={`${draft.originalLength.toLocaleString()} characters selected`}>
        First {draft.text.length.toLocaleString()} characters
      </span>}
      <button className={styles.action} type="button" onClick={() => {
        dismissSelectionElegantly();
        void copySelectionText(draft.text).catch((reason: unknown) => {
          void logCaughtDiagnostic("interface.selection_actions_provider.caught_failure_01", "A handled interface operation failed.", reason, "selection_actions_provider");
          onCopyError?.(reason instanceof Error ? reason : new Error("The selected text could not be copied."));
        });
      }}><Clipboard size={14} /> Copy</button>
      {onAddNote && <button className={styles.action} type="button" onClick={() => {
        dismissSelectionElegantly();
        onAddNote(draft);
      }}><NotebookPen size={14} /> Take note</button>}
      {onRun && ["terminal", "terminal_command"].includes(draft.source.kind) && <button className={styles.action} type="button" onClick={() => {
        dismissSelectionElegantly();
        onRun(draft);
      }}><Play size={14} /> Run</button>}
      <button className={styles.action} type="button" onClick={() => {
        dismissSelectionElegantly();
        onAsk(draft);
      }}><MessageSquareText size={14} /> Ask Nebula</button>
    </div>}
  </SelectionActionsContext.Provider>;
}
