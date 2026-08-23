import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const read = (file) => readFileSync(resolve(root, file), "utf8");
const failures = [];
const authoritative = [
  "src/base.css",
  "src/components-domain.css",
  "src/components-workbench.css",
  "src/components-typography.css",
  "src/components.css",
  "src/calm-responsive.css",
];
const allowedFontSizes = new Set(["11", "12", "13", "14", "15", "16", "20", "28"]);
const allowedRadii = new Set(["0", "8", "10", "16", "999"]);
const typographyFiles = [...new Set([...authoritative, "src/vendor-canvas.css", "src/calm-themes.css"] )];
const allowedFontWeights = new Set([
  "var(--weight-regular)",
  "var(--weight-medium)",
  "var(--weight-semibold)",
  "var(--weight-bold)",
]);

for (const file of authoritative) {
  const css = read(file);
  for (const match of css.matchAll(/font-size\s*:\s*([0-9.]+)px/g)) {
    if (!allowedFontSizes.has(match[1])) failures.push(`${file}: unapproved font-size ${match[1]}px`);
  }
  for (const match of css.matchAll(/border-radius\s*:\s*([0-9.]+)px/g)) {
    if (!allowedRadii.has(match[1])) failures.push(`${file}: unapproved radius ${match[1]}px`);
  }
  for (const match of css.matchAll(/box-shadow\s*:\s*([^;]+);/g)) {
    const value = match[1].trim();
    if (value !== "none" && !value.startsWith("var(--shadow")) failures.push(`${file}: unapproved shadow ${value}`);
  }
  if (/(?:^|[;{]\s*)(?:color|background(?:-color)?|border-color)\s*:\s*(?:#[0-9a-f]{3,8}|rgba?\()/im.test(css)) {
    failures.push(`${file}: raw color outside tokens/themes`);
  }
}

for (const file of typographyFiles) {
  const css = read(file);
  for (const match of css.matchAll(/font-weight\s*:\s*([^;]+);/g)) {
    const value = match[1].trim();
    if (!allowedFontWeights.has(value)) failures.push(`${file}: unapproved font weight ${value}`);
  }
  for (const match of css.matchAll(/font-family\s*:\s*([^;]+);/g)) {
    const value = match[1].trim();
    if (!["var(--font-ui)", "var(--font-mono)"].includes(value)) failures.push(`${file}: unapproved font family ${value}`);
  }
  if (css.includes("var(--mono)")) failures.push(`${file}: retired --mono alias returned`);
}

const tokens = read("src/tokens.css");
for (const contract of [
  '--font-ui: "Geist Variable"',
  '--font-mono: "Geist Mono Variable"',
  "--type-reading: 15px",
]) {
  if (!tokens.includes(contract)) failures.push(`src/tokens.css: typography contract ${contract} is missing`);
}
const bootstrap = read("src/main.tsx");
for (const importPath of ["@fontsource-variable/geist/wght.css", "@fontsource-variable/geist-mono/wght.css"]) {
  if (!bootstrap.includes(importPath)) failures.push(`src/main.tsx: bundled font import ${importPath} is missing`);
}

const themeCss = read("src/calm-themes.css");
for (const property of ["width", "height", "margin", "padding", "position", "inset", "grid-template", "transform", "border-radius"]) {
  if (new RegExp(`(?:^|[;{]\\s*)${property}\\s*:`, "m").test(themeCss)) failures.push(`src/calm-themes.css: theme-specific geometry property ${property}`);
}

const entry = read("src/ui.css");
const layerContract = "@layer tokens, base, components, themes, responsive, vendor-canvas;";
if (!entry.includes(layerContract)) failures.push("src/ui.css: final cascade layer contract is missing");
if (/layer\((?:legacy|workspace)\)/.test(entry)) failures.push("src/ui.css: retired legacy/workspace layer name returned");
for (const retired of ["styles.css", "workspace.css", "refinement.css", "typography.css", "calm-components.css"]) {
  if (entry.includes(`./${retired}`)) failures.push(`src/ui.css: retired stylesheet ${retired} returned`);
}

if (failures.length) {
  console.error(`Calm CSS contract failed (${failures.length}):\n${failures.map((item) => `- ${item}`).join("\n")}`);
  process.exit(1);
}
console.log("Calm CSS contract passed; all imported application styles use the approved product scale.");
