const languageByExtension: Record<string, { id: string; label: string }> = {
  bash: { id: "shell", label: "Shell" }, css: { id: "css", label: "CSS" },
  go: { id: "go", label: "Go" }, htm: { id: "html", label: "HTML" },
  html: { id: "html", label: "HTML" }, java: { id: "java", label: "Java" },
  js: { id: "javascript", label: "JavaScript" }, cjs: { id: "javascript", label: "JavaScript" },
  mjs: { id: "javascript", label: "JavaScript" }, jsx: { id: "javascript", label: "JavaScript" },
  json: { id: "json", label: "JSON" }, md: { id: "markdown", label: "Markdown" },
  py: { id: "python", label: "Python" }, rb: { id: "ruby", label: "Ruby" },
  rs: { id: "rust", label: "Rust" }, sh: { id: "shell", label: "Shell" },
  sql: { id: "sql", label: "SQL" }, toml: { id: "ini", label: "TOML" },
  ts: { id: "typescript", label: "TypeScript" }, tsx: { id: "typescript", label: "TypeScript" },
  yaml: { id: "yaml", label: "YAML" }, yml: { id: "yaml", label: "YAML" },
  zsh: { id: "shell", label: "Shell" },
};

export function languageIdForPath(path: string): string {
  return languageByExtension[path.split(".").pop()?.toLowerCase() ?? ""]?.id ?? "plaintext";
}

export function languageLabelForPath(path: string): string {
  return languageByExtension[path.split(".").pop()?.toLowerCase() ?? ""]?.label ?? "Plain text";
}
