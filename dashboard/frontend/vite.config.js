import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development (npm run dev) API and WebSocket calls are proxied to the FastAPI backend.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
