import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  checkForUpdate,
  getReleaseInfo,
  installAvailableUpdate,
  restartApplication,
  type AvailableUpdate,
  type ReleaseInfo,
} from "../api/updater";

export type ReleaseUpdatePhase =
  | "loading"
  | "idle"
  | "checking"
  | "current"
  | "available"
  | "installing"
  | "restart"
  | "error";

export type ReleaseUpdateFailure = "release" | "check" | "install" | "restart";

export interface ReleaseUpdateContextValue {
  release?: ReleaseInfo;
  availableUpdate?: AvailableUpdate;
  phase: ReleaseUpdatePhase;
  error?: string;
  failure?: ReleaseUpdateFailure;
  dismissed: boolean;
  check: () => Promise<void>;
  install: () => Promise<void>;
  restart: () => Promise<void>;
  dismiss: () => void;
}

const ReleaseUpdateContext = createContext<ReleaseUpdateContextValue | undefined>(undefined);

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error) return error;
  return fallback;
}

export function ReleaseUpdateProvider({ children }: PropsWithChildren) {
  const [release, setRelease] = useState<ReleaseInfo>();
  const [availableUpdate, setAvailableUpdate] = useState<AvailableUpdate>();
  const [phase, setPhase] = useState<ReleaseUpdatePhase>("loading");
  const [error, setError] = useState<string>();
  const [failure, setFailure] = useState<ReleaseUpdateFailure>();
  const [dismissed, setDismissed] = useState(false);
  const mounted = useRef(true);
  const autoCheckStarted = useRef(false);
  const operationInFlight = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const runCheck = useCallback(async (automatic: boolean) => {
    if (operationInFlight.current) return;
    operationInFlight.current = true;
    if (!automatic) setDismissed(false);
    setPhase("checking");
    setError(undefined);
    setFailure(undefined);
    try {
      const update = await checkForUpdate();
      if (!mounted.current) return;
      setAvailableUpdate(update);
      setDismissed(false);
      setPhase(update ? "available" : "current");
    } catch (checkError) {
      if (!mounted.current) return;
      setPhase("error");
      setFailure("check");
      setError(errorMessage(checkError, "Could not check for updates."));
    } finally {
      operationInFlight.current = false;
    }
  }, []);

  const check = useCallback(() => runCheck(false), [runCheck]);

  const install = useCallback(async () => {
    if (operationInFlight.current) return;
    operationInFlight.current = true;
    setDismissed(false);
    setPhase("installing");
    setError(undefined);
    setFailure(undefined);
    try {
      const installed = await installAvailableUpdate();
      if (!mounted.current) return;
      if (installed) {
        setDismissed(false);
        setPhase("restart");
      } else {
        setAvailableUpdate(undefined);
        setPhase("current");
      }
    } catch (installError) {
      if (!mounted.current) return;
      setDismissed(false);
      setPhase("error");
      setFailure("install");
      setError(errorMessage(installError, "Could not install the update."));
    } finally {
      operationInFlight.current = false;
    }
  }, []);

  const restart = useCallback(async () => {
    if (operationInFlight.current) return;
    operationInFlight.current = true;
    setDismissed(false);
    setError(undefined);
    setFailure(undefined);
    try {
      await restartApplication();
    } catch (restartError) {
      if (!mounted.current) return;
      setPhase("error");
      setFailure("restart");
      setError(errorMessage(restartError, "Could not restart Nebula."));
    } finally {
      operationInFlight.current = false;
    }
  }, []);

  const dismiss = useCallback(() => setDismissed(true), []);

  useEffect(() => {
    let active = true;
    void getReleaseInfo()
      .then((info) => {
        if (!active) return;
        setRelease(info);
        setPhase("idle");
        if (!info.updaterEnabled || autoCheckStarted.current) return;
        autoCheckStarted.current = true;
        void runCheck(true);
      })
      .catch((releaseError) => {
        if (!active) return;
        setPhase("error");
        setFailure("release");
        setError(errorMessage(releaseError, "Could not read release information."));
      });
    return () => {
      active = false;
    };
  }, [runCheck]);

  const value = useMemo<ReleaseUpdateContextValue>(() => ({
    release,
    availableUpdate,
    phase,
    error,
    failure,
    dismissed,
    check,
    install,
    restart,
    dismiss,
  }), [availableUpdate, check, dismiss, dismissed, error, failure, install, phase, release, restart]);

  return <ReleaseUpdateContext.Provider value={value}>{children}</ReleaseUpdateContext.Provider>;
}

export function useReleaseUpdate(): ReleaseUpdateContextValue {
  const context = useContext(ReleaseUpdateContext);
  if (!context) throw new Error("useReleaseUpdate must be used inside ReleaseUpdateProvider");
  return context;
}
