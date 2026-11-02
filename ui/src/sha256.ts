import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "@noble/hashes/utils.js";

/** SHA-256 that also works on non-secure LAN HTTP origins without Web Crypto. */
export function sha256Hex(value: string | Uint8Array): string {
  const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value;
  return bytesToHex(sha256(bytes));
}

/** Hash a device file incrementally, including on non-secure LAN origins. */
export async function sha256BlobHex(blob: Blob, signal?: AbortSignal): Promise<string> {
  const hash = sha256.create();
  const reader = blob.stream().getReader();
  try {
    while (true) {
      if (signal?.aborted) throw new DOMException("File hashing was cancelled", "AbortError");
      const {done, value} = await reader.read();
      if (done) break;
      hash.update(value);
    }
  } finally {
    if (signal?.aborted) await reader.cancel();
    reader.releaseLock();
  }
  return bytesToHex(hash.digest());
}
