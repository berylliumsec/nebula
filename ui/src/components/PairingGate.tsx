import { useMemo, useState, type FormEvent, type ReactNode } from "react";

export function PairingGate({ children }: { children: ReactNode }) {
  const pairing = useMemo(() => {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const secret = fragment.get("pair");
    if (!secret) return undefined;
    const code = fragment.get("code") ?? "";
    history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    return { secret, code };
  }, []);
  const [code, setCode] = useState(pairing?.code ?? "");
  const [name, setName] = useState("My phone");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  if (!pairing) return children;

  const redeem = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const response = await fetch("/api/v1/auth/pairings/redeem", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ secret: pairing.secret, confirmation_code: code, name }),
      });
      if (!response.ok) throw new Error((await response.json()).detail ?? "Pairing was rejected.");
      window.location.replace("/");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Pairing failed.");
      setBusy(false);
    }
  };
  return <main className="pairing-gate"><form className="panel" onSubmit={(event) => void redeem(event)}><h1>Pair this device</h1><p>Verify the code shown by Nebula on the desktop before granting this browser access.</p><label>Confirmation code<input required inputMode="numeric" pattern="[0-9]{6}" value={code} onChange={(event) => setCode(event.target.value)} /></label><label>Device name<input required maxLength={200} value={name} onChange={(event) => setName(event.target.value)} /></label>{error && <p role="alert">{error}</p>}<button className="button primary" type="submit" disabled={busy}>{busy ? "Pairing…" : "Pair device"}</button></form></main>;
}
