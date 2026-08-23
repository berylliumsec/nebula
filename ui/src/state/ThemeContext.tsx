import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

export type ThemePreference = "system" | "light" | "dark" | "zero" | "high-contrast";
type ResolvedTheme = Exclude<ThemePreference, "system">;

interface ThemeContextValue {
  preference: ThemePreference;
  resolvedTheme: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
  cycleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | undefined>(undefined);
const STORAGE_KEY = "nebula.theme";
const DEFAULT_PREFERENCE: ThemePreference = "zero";
const preferences: ThemePreference[] = ["system", "light", "dark", "zero", "high-contrast"];

function systemTheme(): ResolvedTheme {
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function initialPreference(): ThemePreference {
  const saved = localStorage.getItem(STORAGE_KEY);
  return preferences.includes(saved as ThemePreference) ? (saved as ThemePreference) : DEFAULT_PREFERENCE;
}

export function ThemeProvider({ children }: PropsWithChildren) {
  const [preference, setPreferenceState] = useState<ThemePreference>(initialPreference);
  const [system, setSystem] = useState<ResolvedTheme>(systemTheme);
  const resolvedTheme = preference === "system" ? system : preference;

  useEffect(() => {
    const query = window.matchMedia?.("(prefers-color-scheme: light)");
    const update = () => setSystem(query?.matches ? "light" : "dark");
    query?.addEventListener("change", update);
    return () => query?.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme;
    document.documentElement.style.colorScheme = resolvedTheme === "light" ? "light" : "dark";
  }, [resolvedTheme]);

  const setPreference = useCallback((value: ThemePreference) => {
    localStorage.setItem(STORAGE_KEY, value);
    setPreferenceState(value);
  }, []);

  const cycleTheme = useCallback(() => {
    setPreference(preference === "light" ? "dark" : preference === "dark" ? "zero" : preference === "zero" ? "high-contrast" : "light");
  }, [preference, setPreference]);

  const value = useMemo(
    () => ({ preference, resolvedTheme, setPreference, cycleTheme }),
    [cycleTheme, preference, resolvedTheme, setPreference],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext);
  if (!context) throw new Error("useTheme must be used inside ThemeProvider");
  return context;
}
