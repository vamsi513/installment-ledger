import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The API has no CORS middleware, so the browser cannot call it cross origin.
// In development the dev server proxies /api to the API instead, which keeps
// the whole front end same origin and needs no change to the backend. In a
// deployment the two are expected to sit behind one origin; point
// VITE_API_BASE_URL somewhere else if they do not.
const API_TARGET = process.env["VITE_API_PROXY_TARGET"] ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
        rewrite: (path: string) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
