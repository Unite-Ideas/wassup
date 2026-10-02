import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the UI runs on :5173 and forwards /api to the Python API on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": process.env.WASSUP_API ?? "http://127.0.0.1:8000" },
  },
  build: { chunkSizeWarningLimit: 2500 },
});
