import { createContext, type Dispatch, type PropsWithChildren, type SetStateAction, useContext } from "react";

export interface ContextualCommand {
  id: string;
  label: string;
  description: string;
  keywords?: string;
  shortcut?: string;
  disabled?: boolean;
  run(): void;
}

export interface ChromeContextValue {
  activityOpen: boolean;
  paletteOpen: boolean;
  sidebarCollapsed: boolean;
  toolbarHost: HTMLElement | null;
  contextualCommands?: ContextualCommand[];
  openPalette: () => void;
  setActivityOpen: Dispatch<SetStateAction<boolean>>;
  setPaletteOpen: Dispatch<SetStateAction<boolean>>;
  setToolbarHost: Dispatch<SetStateAction<HTMLElement | null>>;
  setContextualCommands?: Dispatch<SetStateAction<ContextualCommand[]>>;
  toggleActivity: () => void;
  toggleSidebar: () => void;
}
const ChromeContext = createContext<ChromeContextValue | undefined>(undefined);

export function ChromeProvider({ children, value }: PropsWithChildren<{ value: ChromeContextValue }>) {
  return <ChromeContext.Provider value={value}>{children}</ChromeContext.Provider>;
}

export function useChrome(): ChromeContextValue {
  const context = useContext(ChromeContext);
  if (!context) throw new Error("useChrome must be used inside ChromeProvider");
  return context;
}

export function useOptionalChrome(): ChromeContextValue | undefined {
  return useContext(ChromeContext);
}
