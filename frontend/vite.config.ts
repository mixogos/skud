import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// В режиме разработки (npm run dev) запросы к API уходят на сервер по адресу ниже.
const backend = process.env.SKUD_BACKEND ?? "http://127.0.0.1:8000";
const apiPrefixes = [
  "/auth", "/users", "/groups", "/permissions", "/sessions", "/auditlogs", "/settings",
  "/detect", "/verify", "/watch-lists", "/cards", "/objects", "/camera-groups", "/videos",
  "/events", "/episodes", "/healthz", "/api-docs", "/openapi.json",
];

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      ...Object.fromEntries(apiPrefixes.map((p) => [p, { target: backend, changeOrigin: true }])),
      "/ws": { target: backend, ws: true, changeOrigin: true },
    },
  },
});
