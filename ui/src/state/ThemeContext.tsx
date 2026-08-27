import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

export type ThemePreference = "light" | "dark" | "zero-dark";

interface ThemeContextValue {
  preference: ThemePreference;
  resolvedTheme: ThemePreference;
  setPreference: (preference: ThemePreference) => void;
  cycleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | undefined>(undefined);
const STORAGE_KEY = "nebula.theme";
const DEFAULT_PREFERENCE: ThemePreference = "zero-dark";

function normalizePreference(value: string | null): ThemePreference | undefined {
  if (value === "light" || value === "dark" || value === "zero-dark") return value;
  if (value === "zero" || value === "zero-light") return value === "zero" ? "zero-dark" : "light";
  if (value === "high-contrast" || value === "system") return "dark";
  return undefined;
}

function initialPreference(): ThemePreference {
  const saved = localStorage.getItem(STORAGE_KEY);
  const normalized = normalizePreference(saved) ?? DEFAULT_PREFERENCE;
  if (saved && saved !== normalized) localStorage.setItem(STORAGE_KEY, normalized);
  return normalized;
}

export function ThemeProvider({ children }: PropsWithChildren) {
  const [preference, setPreferenceState] = useState<ThemePreference>(initialPreference);
  const resolvedTheme = preference;

  useEffect(() => {
    const syncStoredPreference = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEY) return;
      const normalized = normalizePreference(event.newValue);
      if (normalized) {
        if (event.newValue !== normalized) localStorage.setItem(STORAGE_KEY, normalized);
        setPreferenceState(normalized);
      }
    };
    window.addEventListener("storage", syncStoredPreference);
    return () => window.removeEventListener("storage", syncStoredPreference);
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
    setPreference(preference === "light" ? "dark" : preference === "dark" ? "zero-dark" : "light");
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
