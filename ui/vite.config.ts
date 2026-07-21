import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const backendHost = process.env.NEBULA_DEV_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
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
