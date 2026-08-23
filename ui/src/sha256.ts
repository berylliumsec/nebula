import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

/** SHA-256 that also works on non-secure LAN HTTP origins without Web Crypto. */
export function sha256Hex(value: string | Uint8Array): string {
  const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value;
  return bytesToHex(sha256(bytes));
}
