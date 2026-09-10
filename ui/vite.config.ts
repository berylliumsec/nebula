import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";

const uiBuild = {
  commit: process.env.NEBULA_BUILD_COMMIT ?? execFileSync("git", ["rev-parse", "HEAD"], {encoding: "utf8"}).trim(),
  builtAt: process.env.NEBULA_BUILD_TIMESTAMP ?? new Date().toISOString(),
  dirty: Boolean(execFileSync("git", ["status", "--porcelain"], {encoding: "utf8"}).trim()),
};

const backendHost = process.env.NEBULA_DEV_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react(), {
    name: "nebula-build-identity",
    enforce: "post",
    generateBundle: {order: "post", handler(_options, bundle) {
      const assets = Object.fromEntries(Object.entries(bundle).map(([name, asset]) => [name,
        createHash("sha256").update(asset.type === "chunk" ? asset.code : asset.source).digest("hex")]));
      // Public assets (notably the service worker) are copied outside the JS
      // bundle but are still part of the release's integrity boundary.
      const includePublic = (directory: string, prefix = "") => {
        for (const entry of readdirSync(directory, {withFileTypes: true})) {
          const name = `${prefix}${entry.name}`;
          const source = path.join(directory, entry.name);
          if (entry.isDirectory()) includePublic(source, `${name}/`);
          else if (entry.isFile()) assets[name] = createHash("sha256").update(readFileSync(source)).digest("hex");
        }
      };
      includePublic(path.resolve(import.meta.dirname, "public"));
      this.emitFile({type: "asset", fileName: "web-build.json", source: JSON.stringify({schema: "nebula.web-build/v1", ...uiBuild, assets}, null, 2)});
    }},
  }],
  define: {__NEBULA_UI_BUILD__: JSON.stringify(uiBuild)},
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    proxy: {
      "/api": {
        target: backendHost,
        changeOrigin: false,
        ws: true,
      },
    },
  },
  envPrefix: ["VITE_", "NEBULA_"],
  build: {
    target: "es2022",
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: "./src/test/setup.ts",
    css: true,
  },
});
