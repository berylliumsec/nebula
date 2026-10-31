import { describe, expect, it } from "vitest";
import { companionOrigin, companionPairingUrl } from "./devicePairingLink";

describe("companion pairing link", () => {
  it("requires an explicit phone-reachable HTTPS origin before a secret is embedded", () => {
    expect(companionOrigin("https://nebula.example:8443", false)).toBe("https://nebula.example:8443");
    for (const unsafe of [
      "http://nebula.example:8443", "https://localhost:8443", "https://127.0.0.2:8443",
      "https://[::1]:8443", "https://0.0.0.0:8443", "https://nebula.example:8443/path",
      "https://nebula.example:8443/?token=x", "https://nebula.example:8443/#fragment",
      "https://user:pass@nebula.example:8443", "nebula.example:8443",
    ]) expect(companionOrigin(unsafe, false)).toBeUndefined();
    expect(companionPairingUrl("https://nebula.example:8443", "one-time-secret", "123456"))
      .toBe("https://nebula.example:8443/#pair=one-time-secret&code=123456");
  });

  it("allows HTTP only when the operator explicitly selected insecure LAN mode", () => {
    expect(companionOrigin("http://192.168.1.44:8000", false)).toBeUndefined();
    expect(companionOrigin("http://192.168.1.44:8000", true)).toBe("http://192.168.1.44:8000");
  });
});
