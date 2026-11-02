/** File bytes stay in the requesting browser until the authenticated Core call. */
export const MAX_LIBRARY_ITEM_BYTES = 20 * 1024 * 1024;

export const ACCEPTED_LIBRARY_FILES = [
  ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".json", ".jsonl",
  ".ndjson", ".html", ".htm", ".pdf", ".docx", ".xlsx", ".py", ".sh",
  ".bash", ".zsh", ".js", ".jsx", ".ts", ".tsx", ".rb", ".go", ".rs",
  ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".pl", ".lua",
  ".sql", ".yaml", ".yml", ".toml", ".xml", ".ini", ".conf",
].join(",");

export function encodeLibraryFile(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

export function startLibraryArtifactDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
  } finally {
    globalThis.setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}
