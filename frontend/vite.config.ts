import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev only: the browser talks to :5173, Vite forwards /api to the FastAPI server.
// In production FastAPI serves the built files itself, so no proxy is involved.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
