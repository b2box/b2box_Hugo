import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// El backend FastAPI sirve el build bajo `/static/`, así que los assets se
// referencian con esa base. En dev, Vite proxya las llamadas de datos al
// backend (uvicorn en :8000) para que fetch('/api/...') funcione igual que en prod.
//
// OJO: NO proxyamos GET `/login` porque esa ruta es una página del SPA (React
// Router). El endpoint de login del backend vive en POST `/api/login`.
const BACKEND = process.env.HUGO_BACKEND ?? "http://localhost:8000";

export default defineConfig({
  base: "/static/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": BACKEND,
      "/audit-log": BACKEND,
      "/audit": BACKEND,
      "/verify": BACKEND,
      "/health": BACKEND,
      "/products": BACKEND,
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
